#!/bin/env python3.12
from typing import Any

from pydantic import ConfigDict

from ampav.core.logging import LOG_FORMAT
from ampav.core.async_tool import AsyncTool, AsyncJobStatus, AsyncStatusCode
import argparse
from azure.identity import ClientSecretCredential, DefaultAzureCredential
import logging
import os
import requests
import time

from ampav.core.schema.basemodel import AmpAVBaseModel
from ampav.core.schema.tool import ToolOutput
from .vi_models import JobStatus, RawVideoIndexer, JobState
from urllib.parse import urlparse
import yaml
import json


# chunks shamelessly stolen from 
# https://github.com/Azure-Samples/azure-video-indexer-samples/blob/master/API-Samples/Python/


class AzureVideoIndexer(AsyncTool):
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
            
            # initialize the rest of state
            self.vi_subscription_id: str = vi_subscription_id
            self.vi_resource_group: str = vi_resource_group
            self.vi_account_name: str = vi_account_name
            self.auth_token_expire: float = 0
            self.auth_token: str = None
            self.api_url_base: str = None

            self._get_access_token()

            
            
    def _get_access_token(self):
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
            
            
    def submit(self, video_url: str, **kwargs) -> str:
        """Submit a job to AVI and return the job id"""        
        
        # submit the job.      
        submit_url = self.api_url_base + "/Videos"
        # https://api-portal.videoindexer.ai/api-details#api=Operations&operation=Upload-Video
        # https://api.videoindexer.ai/{location}/Accounts/{accountId}/Videos
        # ?name={name}[&privacy][&priority][&description][&partition][&externalId][&externalUrl][&callbackUrl][&metadata][&language][&videoUrl][&fileName][&excludedAI][&isSearchable][&indexingPreset][&streamingPreset][&linguisticModelId][&personModelId][&sendSuccessEmail][&brandsCategories][&customLanguages][&logoGroupId][&useManagedIdentityToDownloadVideo][&preventDuplicates][&retentionPeriod][&punctuationMode][&profanityFilterMode][&accessToken]
        # https://api.videoindexer.ai/{location}/Accounts/{accountId}/Videos/{videoId}/Thumbnails/{thumbnailId}
        # language:  auto, multi
        # if videoUrl is not specified, the file can be sent as multipart/form body
        # fileName

        params = {'name': video_url.rsplit('/', 1)[-1],
                  'accessToken': self._get_access_token(),
                  **kwargs}
                            
        parsed_url = urlparse(video_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            # this is a local URL, so it can be sent as multipart/form body
            with open(parsed_url.path, "rb") as f:
                r = requests.post(submit_url, params=params,
                                  files={(parsed_url.path.rsplit('/')[-1], f)})            
        else:            
            params['videoUrl'] = video_url
            print(submit_url, params)
            r = requests.post(submit_url, params=params)

        r.raise_for_status()
        job = JobStatus(**r.json())
        return job.id
    

    def _jobs(self) -> dict[str, JobStatus]:
        "Get information about the jobs in AVI"
        res = {}
        nextpage = {}
        while True:
            check_url = f"{self.api_url_base}/Videos"
            r = requests.get(check_url,
                            params={'accessToken': self._get_access_token(),
                                    **nextpage})
            r.raise_for_status()
            data = r.json()
            
            for r in data['results']:
                res[r['id']] = JobStatus(**r)            
            nextpage = data['nextPage']
            if data['nextPage']['done']:
                break
        return res


    def get_status(self, job_id) -> AsyncJobStatus:
        """Get information about a specific job"""
        j = self._jobs()
        if job_id in j:
            job = j[job_id]            
            state_map = {JobState.UPLOADED: (AsyncStatusCode.IN_PROGRESS, 0),
                         JobState.PROCESSING: (AsyncStatusCode.IN_PROGRESS, float(job.processingProgress.replace('%', ''))),
                         JobState.PROCESSED: (AsyncStatusCode.FINISHED, 100),
                         JobState.FAILED: (AsyncStatusCode.ERROR, 100)}
            return AsyncJobStatus(job_id=job_id,
                                  status=state_map[job.state][0],
                                  progress=state_map[job.state][1])
        else:
            raise KeyError(f"Job id {job_id} doesn't exist")
  

    def is_done(self, job_id: str) -> bool:
        return job_id in self._jobs()


    def get_result(self, job_id: str) -> ToolOutput | None:
        "Check on the status and handle results if ready"
        
        job = self.get_status(job_id)        
        if job.progress < 100:
            # not done yet.
            return None
        
        # get the video indexer information
        r = requests.get(url=f"{self.api_url_base}/Videos/{job.job_id}/Index",
                         params={
                            'accessToken': self._get_access_token(),
                            'language': 'English',
                            'includeSummarizedInsights': 'true',
                        })       
        r.raise_for_status()
        res = {'data': r.json(),
               'thumbnails': {}}
        # look for other artifacts
        for artifact in ('ocr', 'faces'):
            r = requests.get(url=f"{self.api_url_base}/Videos/{job_id}/ArtifactUrl", 
                             params={'type': artifact,
                                     'accessToken': self._get_access_token()})            
            if r.status_code == 200:
                artifact_url = r.text.strip('"')
                
                print(f"Found {artifact} artifact at {artifact_url}")
                r = requests.get(url=artifact_url)
                print(f"Artifact mime-type: {r.headers['Content-Type']}")
                try:
                    res[artifact] = json.loads(r.content)
                except:
                    res[artifact] = r.content
        # https://api.videoindexer.ai/{location}/Accounts/{accountId}/Videos/{videoId}/Thumbnails/{thumbnailId}
        # find all the thumbnails
        thumb_base = f"{self.api_url_base}/Videos/{job_id}/Thumbnails"
        for thumbId in set(key_finder(res['data'], 'thumbnailId')):
            r = requests.get(url=f"{thumb_base}/{thumbId}", 
                             params={'accessToken': self._get_access_token()})            
            if r.status_code == 200:
                res['thumbnails'][thumbId] = r.content
            else:
                logging.warning(f"Cannot retrieve thumbnail {thumbId}")

        return res


    def wait_until_done(self, job_id, check_interval=30):
        """Poll the cloud job until the job finishes"""
        while (r := self.check(job_id)) is None:
            time.sleep(check_interval)
        return r


    def cleanup(self, job_id: str):
        """Delete an AVI job"""        
        logging.info(f"Removing Video Indexer Job {job_id}")
        video_url = f"{self.api_url_base}/Videos/{job_id}"
        requests.delete(url=video_url,
                        params={'accessToken': self._get_access_token()})
        


def key_finder(data: Any, key: str) -> list:
    """Find the values for the given key no matter where
       it is in the data structure"""
    res = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key:
                res.append(v)
            else:
                if isinstance(v, dict):
                    res.extend(key_finder(v, key))
                elif isinstance(v, (list, set, tuple)):
                    for i in v:
                        res.extend(key_finder(i, key))
    elif isinstance(data, (set, list, tuple)):
        for i in data:
            res.extend(key_finder(i, key))

    return res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action='store_true', help="Turn on debug logging")
    parser.add_argument('--vi_account_name', type=str, help="VideoIndexer Account Name (default: environment)")
    parser.add_argument('--vi_subscription_id', type=str, help='VideoIndexer Subscription ID (default: environment)')
    parser.add_argument('--vi_resource_group', type=str, help="VideoIndexer Resource Group (default: environment)")
    parser.add_argument('--azure_client_id', type=str, help='Azure Client ID (default: environment or az login)')
    parser.add_argument('--azure_tenant_id', type=str, help='Azure Tenant ID (default: environment or az login)')
    parser.add_argument('--azure_client_secret', type=str, help='Azure Client Secret (default: environment or az login)')
    
    subp = parser.add_subparsers(dest="command", help="Sub-commands", required=True)
    cmd = subp.add_parser("list", help="List Video Indexer jobs")
    cmd.add_argument("--format", choices=['yaml', 'json'], default='yaml', help="Output format")

    cmd = subp.add_parser("delete", help="Delete Video Indexer job")
    cmd.add_argument("job_id", help="Job ID to delete")

    cmd = subp.add_parser("delete_all", help="Delete all Video Indexer Jobs")

    cmd = subp.add_parser("submit", help="Submit a new Video Indexer Job")
    cmd.add_argument("video_url", help="Video URL")

    cmd = subp.add_parser("run", help="Run a new Video Indexer Job and wait for the results")
    cmd.add_argument("video_url", help="Video URL")
    cmd.add_argument("--format", choices=["yaml", "json"], default="yaml", help="Output Format if waiting")

    cmd = subp.add_parser("status", help="Retrive a Video Indexer Job Status")
    cmd.add_argument("job_id", help="Job Id to retrieve")
    cmd.add_argument("--format", choices=["yaml", "json"], default="yaml", help="Output Format")

    cmd = subp.add_parser("retrieve", help="Retrive a Video Indexer Job Result")
    cmd.add_argument("job_id", help="Job Id to retrieve")
    cmd.add_argument("--format", choices=["yaml", "json"], default="yaml", help="Output Format")

    cmd = subp.add_parser("parseraw", help="Parse a raw videoindexer output to ampav objects")
    cmd.add_argument("rawfile", help="Raw file data")

    args = parser.parse_args()

    logging.basicConfig(format=LOG_FORMAT, level=logging.DEBUG if args.debug else logging.INFO)
    logging.getLogger('azure').setLevel(logging.INFO if args.debug else logging.WARNING)

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

    match args.command:
        case "list":
            out = {}
            for k, v in vi._jobs().items():
                out[k] = v.model_dump()
            if args.format == "yaml":
                print(yaml.safe_dump(out))
            else:
                print(json.dumps(out))

        case "delete":
            vi.cleanup(args.job_id)
        
        case "delete_all":
            for k in vi._jobs().keys():
                vi.cleanup(k)

        case "submit":
            job_id = vi.submit(args.video_url)        
            print(job_id)

        case "run":
            result = vi.run(args.video_url)

            if args.format == "yaml":
                print(yaml.safe_dump(result))
            else:
                print(json.dumps(result))
                        
        case "status":
            result = vi.get_status(args.job_id)
            if args.format == "yaml":
                print(yaml.safe_dump(result.model_dump()))
            else:
                print(json.dumps(result.model_dump()))            

        case "retrieve":
            result = vi.get_result(args.job_id)
            if result is None:
                logging.info("The job is not ready yet")
            else:
                if args.format == "yaml":
                    print(yaml.safe_dump(result))
                else:
                    print(json.dumps(result))            

        case "parseraw":
            AmpAVBaseModel.model_config = ConfigDict(extra="forbid")
            with open(args.rawfile) as f:
                data = yaml.safe_load(f)
            
            print(key_finder(data, 'thumbnailId'))
            vidata = RawVideoIndexer(**data)
            print(vidata.model_dump_yaml())


if __name__ == "__main__":
    main()
