#!/bin/env python3.12
from pathlib import Path

from ampav.core.logging import LOG_FORMAT
from ampav.core.async_tool import AsyncTool, AsyncJobStatus, AsyncStatusCode, ToolError
import argparse
from azure.identity import ClientSecretCredential, DefaultAzureCredential
import logging
import os
import requests
import time

from ampav.core.schema.tool import ToolOutput
from ampav.core.utils import dump_data, load_data
from .vi_models import JobStatus, JobState, ViRawData
from .vi_utils import key_finder, parse_vi_data
from urllib.parse import urlparse

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

        params = kwargs
        params.update({'name': video_url.rsplit('/', 1)[-1],
                       'accessToken': self._get_access_token()})    
                            
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


    def list_jobs(self) -> list[AsyncJobStatus]:
        """Return a list of job status info for all jobs known by the implementation
        
        Note: The implementation should restrict the returned jobs to ones that the
        library tool has created, but this is not guaranteed.
        """
        res: list[AsyncJobStatus] = []
        for k, v in self._jobs().items():
            res.append(AsyncJobStatus(job_id=k,
                                      status={JobState.UPLOADED: AsyncStatusCode.QUEUED,
                                              JobState.PROCESSING: AsyncStatusCode.IN_PROGRESS,
                                              JobState.PROCESSED: AsyncStatusCode.SUCCEEDED,
                                              JobState.FAILED: AsyncStatusCode.FAILED}[v.state],
                                      progress=float(v.processingProgress.replace('%', '')),
                                      message=None))
        return res


    def get_status(self, job_id: str, details: bool = True) -> AsyncJobStatus:  
        """ Return progress/status information for a job.

        Implementors may include additional provider-specific details when 
        `details` is true.

        If the job doesn't exist, a KeyError will be raised

        Note:  The default value of `details` may vary from tool to tool.
        """

        for j in self.list_jobs():
            if j.job_id == job_id:
                return j
        return KeyError(f"Job id {job_id} doesn't exist")
    
    
    def get_result(self, job_id: str) -> ToolOutput | None:
        """Return AMPAV tool output when ready, otherwise return None.

        When the result has been successfully retrieved the job will be
        cleaned up.

        If the job doesn't exist, a KeyError will be raised.

        Failed jobs will be cleaned up and raise a ToolError with relevant details.
        """        
        job = self.get_status(job_id)        
        if job.status in (AsyncStatusCode.QUEUED, AsyncStatusCode.IN_PROGRESS):
            # not done yet.
            return None
        
        if job.status == AsyncStatusCode.FAILED:
            self.cleanup(job_id)
            raise ToolError("The job has failed")
        
        # get the video indexer information
        r = requests.get(url=f"{self.api_url_base}/Videos/{job.job_id}/Index",
                         params={
                            'accessToken': self._get_access_token(),
                            'language': 'English',
                            'includeSummarizedInsights': 'true',
                        })       
        r.raise_for_status()
        res = {'format': 'viraw',
               'data': r.json(),
               'thumbnails': {}}
        # look for other artifacts
        for artifact in ('ocr', 'faces'):
            r = requests.get(url=f"{self.api_url_base}/Videos/{job_id}/ArtifactUrl", 
                             params={'type': artifact,
                                     'accessToken': self._get_access_token()})            
            if r.status_code == 200:
                artifact_url = r.text.strip('"')
                
                r = requests.get(url=artifact_url)
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

        self._rawdata = ViRawData(**res)
        return AzureVideoIndexer.native_to_tool_output(res)


    @staticmethod
    def native_to_tool_output(native: dict) -> ToolOutput:
        """Convert a native result data structure (such as raw AWS Transcribe
        data) into an AMPAV ToolOutput."""

        return parse_vi_data(native)


    def cleanup(self, job_id: str) -> None:
        """Clean up temporary resources created by this job.
        
        * If the job_id doesn't exist, do nothing
        * If the job is queued, dequeue it and clean up
        * If the job is running, stop the job and clean up
        * If the job has finished, clean up resources.

        This call is blocking and will wait until finished.  If a native job
        appears to be hung this method may raise an exception.
        """
        try:
            status = self.get_status(job_id)
        except KeyError:
            return

        logging.info(f"Removing Video Indexer Job {job_id}")
        video_url = f"{self.api_url_base}/Videos/{job_id}"
        requests.delete(url=video_url,
                        params={'accessToken': self._get_access_token()})


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

    cmd = subp.add_parser("process", help="Run a new Video Indexer Job and wait for the results")
    cmd.add_argument("video_url", help="Video URL")    
    cmd.add_argument("--format", choices=['yaml', 'json', 'pickle'], default='yaml', help="Output format")
    cmd.add_argument("--output", type=Path, help="Output file")

    cmd = subp.add_parser("status", help="Retrive a Video Indexer Job Status")
    cmd.add_argument("job_id", help="Job Id to retrieve")
    cmd.add_argument("--format", choices=['yaml', 'json'], default='yaml', help="Output format")

    cmd = subp.add_parser("retrieve", help="Retrive a Video Indexer Job Result")
    cmd.add_argument("job_id", help="Job Id to retrieve")
    cmd.add_argument("--format", choices=['yaml', 'json', 'pickle'], default='yaml', help="Output format")
    cmd.add_argument("--output", type=Path, help="Output file")
    
    cmd = subp.add_parser("dumpraw", help="Dump a raw videoindexer output (for debugging only)")
    cmd.add_argument("job_id", help="Job Id to dump")
    cmd.add_argument("--format", choices=['yaml', 'pickle'], default='yaml', help="Output format")
    cmd.add_argument("--output", type=Path, help="Output file")
    
    cmd = subp.add_parser("parseraw", help="Parse a raw videoindexer output to ampav objects (for debugging only)")
    cmd.add_argument("rawfile", type=Path, help="Raw file data")
    cmd.add_argument("--format", choices=['yaml', 'json', 'pickle'], default='yaml', help="Output format")
    cmd.add_argument("--output", type=Path, help="Output file")
    
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
            dump_data(out, args.format, None)

        case "delete":
            vi.cleanup(args.job_id)
        
        case "delete_all":
            for k in vi._jobs().keys():
                vi.cleanup(k)

        case "submit":
            job_id = vi.submit(args.video_url)        
            print(job_id)

        case "process":
            result = vi.process(args.video_url)
            dump_data(result, args.format, args.output)
                        
        case "status":
            result = vi.get_status(args.job_id)
            dump_data(result, args.format, None)

        case "retrieve":
            result = vi.get_result(args.job_id)
            if result is None:
                logging.info("The job is not ready yet")
            else:
                dump_data(result, args.format, args.output)

        case "dumpraw":
            result = vi.get_result(args.job_id)
            logging.info("Got the results.")
            if result is None:
                logging.info("The job is not ready yet")
            else:
                dump_data(vi._rawdata, args.format, args.output)

        case "parseraw":          
            data = load_data(args.rawfile)
            if isinstance(data, dict):
                data = ViRawData(**data)
            vidata = AzureVideoIndexer.native_to_tool_output(data.model_dump())
            dump_data(vidata, args.format, args.output)


if __name__ == "__main__":
    main()
