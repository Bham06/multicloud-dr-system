import os 
from google.cloud import storage as gcs_storage
from google.cloud import secretmanager 
import boto3
import json

def get_secret(project_id, secret_id, version_id="latest"):
    '''Retrieve secrets from secret manager'''
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
    response = client.access_secret_version(request={"name":name})
    return response.payload.data.decode("UTF-8")

def sync_file(event, context):
    '''Sync file using object finalize event'''
    file_name = event['name']
    bucket_name = event['bucket']
    
    print(f"Triggered for file: {file_name} in bucket: {bucket_name}")
    
    project_id = os.environ.get('GCP_PROJECT')
    
    # Retrieve AWS credentials
    try:
        aws_creds = json.loads(get_secret(project_id, 'aws-credentials'))
        aws_access_key = aws_creds['access_key_id']
        aws_secret_key = aws_creds['secret_access_key']
        aws_region = aws_creds.get('region', 'us-east-1')
    except Exception as e:
        print(f"Error retreiving AWS credentials: {str(e)}")
        raise
    
    # Download from GCS
    try:
        gcs_client = gcs_storage.Client()
        bucket = gcs_client.bucket(bucket_name)
        blob = bucket.blob(file_name)
        file_data = blob.download_as_bytes()
        print(f"Download {len(file_data)} bytes from GCS")
    except Exception as e:
        print(f"Error downloadingfrom GCS: {str(e)}")
        raise
    
    # Upload to S3
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=aws_region
        )
        
        s3_bucket = os.environ.get('S3_BUCKET_NAME')
        
        s3_client.put_object(
            Bucket=s3_bucket,
            Key=file_name,
            Body=file_data,
            Metadata={
                'source': 'gcs',
                'original_bucket': bucket_name
            }
        )
        print(f"Successfully synced {file_name} to S3 bucket {s3_bucket}")
    
    except Exception as e:
        print(f"Error syncing {file_name} to S3: {str(e)}")
        raise