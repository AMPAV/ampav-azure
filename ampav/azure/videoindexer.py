#!/bin/env python3.12
from ampav.core.logging import LOG_FORMAT
import argparse
from azure.identity import ClientSecretCredential, DefaultAzureCredential
import boto3
import logging
import os
import requests
import time
from .vi_models import JobStatus
from urllib.parse import urlparse
import yaml

# chunks shamelessly stolen from 
# https://github.com/Azure-Samples/azure-video-indexer-samples/blob/master/API-Samples/Python/


class AzureVideoIndexer:
    def __init__(self, vi_subscription_id: str, vi_resource_group: str, 
                 vi_account_name: str,
                 azure_tenant_id: str=None, azure_client_id: str=None,
                 azure_client_secret: str=None):
            """Initialize Video Indexer credentials
            
            If the azure_* parameters are set we'll use them, otherwise the
            authentication try to use other authentication methods (such as
            Environment, Azure CLI, etc)
            
            """
            # Get our primary credential.
            if azure_tenant_id is not None:
                self.credential = ClientSecretCredential(azure_tenant_id, azure_client_id, azure_client_secret)
            else:
                self.credential = DefaultAzureCredential()            
            if not self.credential:
                raise Exception("Cannot validate credentials")
            
            self.get_access_token()

            # initialize the rest of state
            self.vi_subscription_id: str = vi_subscription_id
            self.vi_resource_group: str = vi_resource_group
            self.vi_account_name: str = vi_account_name
            self.auth_token_expire: float = 0
            self.auth_token: str = None
            self.api_url_base: str = None

            
            
    def get_access_token(self):
        """Get the authentication token, regenerating it if needed"""
        if time.time() > self.auth_token_expire or self.auth_token is None:
            logging.debug("Requesting new auth token")
            # Get an ARM access token.  We'll just get a default one.
            arm_access_token = self.credential.get_token("https://management.azure.com/.default").token
            logging.debug(f"Retrieved ARM token: {arm_access_token}")

            # We have to get a video indexer account access token...by building 
            # an obnoxious URL
            api_url = 'https://management.azure.com'
            api_url += f'/subscriptions/{self.vi_subscription_id}'
            api_url += f'/resourceGroups/{self.vi_resource_group}'
            api_url += '/providers/Microsoft.VideoIndexer'
            api_url += f'/accounts/{self.vi_account_name}'
            
            # get the access token
            response = requests.post(f"{api_url}/generateAccessToken?api-version=2024-01-01",
                                     json={'permissionType': 'Contributor', 'scope': 'Account'},
                                     headers={'Authorization': f"Bearer {arm_access_token}",
                                              'Content-Type': 'application/json'})
            response.raise_for_status()                
            self.auth_token = response.json()['accessToken']                
            self.auth_token_expire = time.time() + 1800  # 30 minutes
            logging.debug(f"Retrieved VI Account Access Token: {self.auth_token}")

            # while we're here, we're going to grab the account information so
            # we can get the region and accountid automatically and generate the API URL
            response = requests.get(f"{api_url}?api-version=2024-01-01",
                                     headers={'Authorization': f'Bearer {arm_access_token}',
                                              'Content-Type': 'application/json'})
            response.raise_for_status()
            account = response.json()
            # set up our api_url_base
            if self.api_url_base is None:
                self.api_url_base = f"https://api.videoindexer.ai/{account['location']}/Accounts/{account['properties']['accountId']}"
        return self.auth_token        
            
            
    def submit(self, video_url: str, **kwargs):
        """Submit a job to AVI and return the job id"""        
        parsed_url = urlparse(video_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            raise ValueError("URL must be a network url")
        
        # submit the job.      
        submit_url = self.api_url_base + "/Videos"
        # https://api-portal.videoindexer.ai/api-details#api=Operations&operation=Upload-Video
        # https://api.videoindexer.ai/{location}/Accounts/{accountId}/Videos
        # ?name={name}[&privacy][&priority][&description][&partition][&externalId][&externalUrl][&callbackUrl][&metadata][&language][&videoUrl][&fileName][&excludedAI][&isSearchable][&indexingPreset][&streamingPreset][&linguisticModelId][&personModelId][&sendSuccessEmail][&brandsCategories][&customLanguages][&logoGroupId][&useManagedIdentityToDownloadVideo][&preventDuplicates][&retentionPeriod][&punctuationMode][&profanityFilterMode][&accessToken]
        # https://api.videoindexer.ai/{location}/Accounts/{accountId}/Videos/{videoId}/Thumbnails/{thumbnailId}
        # language:  auto, multi
        # if videoUrl is not specified, the file can be sent as multipart/form body
        # fileName

        r = requests.post(submit_url,
                            params={
                            'name': video_url.rsplit('/', 1)[-1] + "-" + str(int(time.time())),
                            'accessToken': self.get_access_token(),
                            'videoUrl': video_url,
                            **kwargs
                            })
        r.raise_for_status()
        job = JobStatus(**r.json())
        #print("Submission response:", job)
        return job.id
    

    def jobs(self) -> dict[str, JobStatus]:
        "Get information about the jobs in AVI"
        res = {}
        nextpage = {}
        while True:
            check_url = f"{self.api_url_base}/Videos"
            r = requests.get(check_url,
                            params={'accessToken': self.get_access_token(),
                                    **nextpage})
            r.raise_for_status()
            data = r.json()
            
            for r in data['results']:
                res[r['id']] = JobStatus(**r)            
            nextpage = data['nextPage']
            if data['nextPage']['done']:
                break
        return res


    def job_info(self, job_id) -> JobStatus:
        """Get information about a specific job"""
        j = self.jobs()
        if job_id in j:
            return j[job_id]
        else:
            return None
  

    def check(self, job_id) -> dict | None:
        "Check on the status and handle results if ready"
        # get the job data.
        job = self.job_info(job_id)        
        if job.state in ('Uploaded', 'Processing'):
            return None
        
        # write the Azure Video Index data to a file
        r = requests.get(url=f"{self.api_url_base}/Videos/{job.id}/Index",
                         params={
                            'accessToken': self.get_access_token(),
                            'language': 'English',
                            'includeSummarizedInsights': 'true',
                        })       
        r.raise_for_status()
        res = {'data': r.json()}
        # look for other artifacts
        for artifact in ('ocr', 'faces'):
            r = requests.get(url=f"{self.api_url_base}/Videos/{job_id}/ArtifactUrl", 
                             params={'type': artifact,
                                     'accessToken': self.get_access_token()})            
            if r.status_code == 200:
                artifact_url = r.text.strip('"')
                
                print(f"Found {artifact} artifact at {artifact_url}")
                r = requests.get(url=artifact_url)
                res[artifact] = r.content

        return res


    def wait_until_done(self, job_id, check_interval=30):
        """Poll the cloud job until the job finishes"""
        while (r := self.check(job_id)) is None:
            time.sleep(check_interval)
        return r


    def cleanup(self, job_id):
        """Delete an AVI job"""        
        logging.info(f"Removing Video Indexer Job {job_id}")
        video_url = f"{self.api_url_base}/Videos/{job_id}"
        requests.delete(url=video_url,
                        params={'accessToken': self.get_access_token()})
        

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action='store_true', help="Turn on debug logging")
    parser.add_argument('--vi_account_name', type=str, help="VideoIndexer Account Name (default: environment)")
    parser.add_argument('--vi_subscription_id', type=str, help='VideoIndexer Subscription ID (default: environment)')
    parser.add_argument('--vi_resource_group', type=str, help="VideoIndexer Resource Group (default: environment)")
    parser.add_argument('--azure_client_id', type=str, help='Azure Client ID (default: environment or az login)')
    parser.add_argument('--azure_tenant_id', type=str, help='Azure Tenant ID (default: environment or az login)')
    parser.add_argument('--azure_client_secret', type=str, help='Azure Client Secret (default: environment or az login)')
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--video_url", type=str, help="Video to submit for processing")
    group.add_argument("--list", action="store_true", help="List the VI jobs")
    group.add_argument("--delete", type=str, help="Delete a VI job")
    group.add_argument("--delete_all", action="store_true", help="Delete all VI jobs")
    args = parser.parse_args()

    logging.basicConfig(format=LOG_FORMAT, level=logging.DEBUG if args.debug else logging.INFO)

    # copy any environment settings to our variables.  This sort of duplicates
    # what happens in the environment credential chain for azure, but it's a
    # reasonable choice for a cli.
    for x in ('vi_account_name', 'vi_subscription_id', 'vi_resource_group',
              'azure_client_id', 'azure_tenant_id', 'azure_client_secret'):
        if getattr(args, x) is None and x.upper() in os.environ:
            setattr(args, x, os.environ[x.upper()])

    vi = AzureVideoIndexer(args.vi_subscription_id, args.vi_resource_group,
                           args.vi_account_name, args.azure_tenant_id,
                           args.azure_client_id, args.azure_client_secret)

    if args.list:
        for k, v in vi.jobs().items():
            print(k, v)
    elif args.delete:
        vi.cleanup(args.delete)
    elif args.delete_all:
        for k, v in vi.jobs().items():
            vi.cleanup(k)
    elif args.video_url:
        job_id = vi.submit(args.video_url, 
                        metadata={'title': 'Nothing to see here',
                                    'director': 'Alan Smithee'},
                        externalId="some external id",
                        description="A test from here")        
        logging.info(f"Submitted job: {job_id}")
        while (result := vi.check(job_id)) is None:
            js = vi.job_info(job_id)
            logging.info(f"Status: {js.processingProgress}, {js.state}")
            time.sleep(10)

        result = vi.wait_until_done(job_id)

        print(yaml.safe_dump(result))
        
        vi.cleanup(job_id)
    else:
        logging.error("How did we get here?")

if __name__ == "__main__":
    main()
