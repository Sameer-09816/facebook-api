# main.py
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
import requests
import os
import mimetypes
import urllib.parse
import json # Ensure json is imported for json.loads and JSONDecodeError

# Create the FastAPI app instance
# Vercel will look for this 'app' object.
app = FastAPI(
    title="TeleSocial Proxy API",
    description="A proxy API to interact with the tele-social.vercel.app downloader. "
                "Access the interactive API documentation at /docs or /redoc.",
    version="1.0.2"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows all origins
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods
    allow_headers=["*"], # Allows all headers
)

# Synchronous helper function to perform the blocking network request
def _fetch_from_tele_social_sync(target_url: str, api_base_url: str, params: dict):
    try:
        upstream_response = requests.get(api_base_url, params=params, timeout=90, stream=True)
        if upstream_response.status_code >= 400:
            return {"type": "error_upstream", "response_object": upstream_response}
        return {"type": "success", "response_object": upstream_response}
    except requests.exceptions.Timeout:
        return {"type": "exception", "exception_type": "Timeout", "detail": f"Request to upstream API timed out for URL: {target_url}"}
    except requests.exceptions.ConnectionError:
        return {"type": "exception", "exception_type": "ConnectionError", "detail": f"Could not connect to upstream API for URL: {target_url}"}
    except requests.exceptions.RequestException as e:
        return {"type": "exception", "exception_type": "RequestException", "detail": f"Upstream API request error for {target_url}: {str(e)}"}

async def get_content_from_tele_social(target_url: str):
    api_base_url = "https://tele-social.vercel.app/down"
    params = {'url': target_url}
    result_dict = await run_in_threadpool(_fetch_from_tele_social_sync, target_url, api_base_url, params)

    if result_dict["type"] == "exception":
        if result_dict["exception_type"] == "Timeout":
            raise HTTPException(status_code=504, detail=result_dict["detail"])
        elif result_dict["exception_type"] == "ConnectionError":
            raise HTTPException(status_code=503, detail=result_dict["detail"])
        else: # RequestException
            raise HTTPException(status_code=500, detail=result_dict["detail"])
    return result_dict["response_object"]

def _parse_json_and_close_sync(resp: requests.Response):
    try:
        data = resp.json()
        return data
    finally:
        resp.close()

def _read_text_and_close_sync(resp: requests.Response):
    try:
        text_data = resp.text
        return text_data
    finally:
        resp.close()

@app.get("/download/")
async def download_content_via_proxy(
    url: str = Query(..., 
                     description="The URL of the content you want the tele-social API to process (e.g., Instagram post URL, TikTok video URL).",
                     examples=["https://www.instagram.com/p/Cxyz1234/"])
):
    if not url:
        raise HTTPException(status_code=400, detail="The 'url' query parameter is required.")

    upstream_response = await get_content_from_tele_social(url)
    response_status_code = upstream_response.status_code
    content_type = upstream_response.headers.get('content-type', '').lower()
    
    if response_status_code >= 400: # Upstream error
        try:
            error_body_text = await run_in_threadpool(_read_text_and_close_sync, upstream_response)
            try:
                error_json = json.loads(error_body_text)
                return JSONResponse(content=error_json, status_code=response_status_code)
            except json.JSONDecodeError:
                return JSONResponse(
                    content={"error": "Upstream API error", "details": error_body_text, "upstream_status_code": response_status_code}, 
                    status_code=response_status_code
                )
        except Exception as e:
            print(f"Error: Failed to read or process error response body from upstream API for URL {url}. Error: {str(e)}")
            raise HTTPException(status_code=502, detail=f"Failed to read or process error response from upstream API: {str(e)}")

    if 'application/json' in content_type: # Successful JSON response
        try:
            json_data = await run_in_threadpool(_parse_json_and_close_sync, upstream_response)
            return JSONResponse(content=json_data, status_code=response_status_code)
        except (requests.exceptions.JSONDecodeError, json.JSONDecodeError) as e:
            print(f"Error: Upstream API (URL: {url}) declared Content-Type: application/json but failed to provide valid JSON. Upstream Decode Error: {str(e)}")
            raise HTTPException(status_code=502, detail="Upstream API returned malformed JSON despite declaring JSON content type.")
        except Exception as e:
            print(f"Error: Unexpected issue processing successful JSON response for URL {url}. Error: {str(e)}")
            raise HTTPException(status_code=500, detail="Server error while processing upstream JSON response.")
    else: # Stream as file
        response_headers = {}
        if 'content-type' in upstream_response.headers:
            response_headers['Content-Type'] = upstream_response.headers['content-type']
        
        if 'content-disposition' in upstream_response.headers:
            response_headers['Content-Disposition'] = upstream_response.headers['content-disposition']
        else:
            try:
                parsed_target_url = urllib.parse.urlparse(url)
                path_basename = os.path.basename(parsed_target_url.path)
                filename = "downloaded_file"
                if path_basename:
                    filename_stem = path_basename.split("?")[0].split("#")[0]
                    guessed_extension = mimetypes.guess_extension(response_headers.get('Content-Type', 'application/octet-stream'))
                    if guessed_extension and not filename_stem.lower().endswith(guessed_extension.lower()):
                        filename = f"{filename_stem}{guessed_extension}"
                    else:
                        filename = filename_stem
                filename = "".join(c for c in filename if c.isalnum() or c in ['.', '_', '-']).strip()
                if not filename: filename = "downloaded_file"
                response_headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            except Exception:
                response_headers['Content-Disposition'] = 'attachment; filename="downloaded_file"'

        if 'content-length' in upstream_response.headers:
            response_headers['Content-Length'] = upstream_response.headers['content-length']

        async def file_streaming_generator(resp: requests.Response, chunk_size=1024*64):
            try:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    yield chunk
            finally:
                await run_in_threadpool(resp.close)

        return StreamingResponse(
            file_streaming_generator(upstream_response),
            status_code=response_status_code,
            headers=response_headers
        )

# The if __name__ == "__main__": block for local uvicorn execution is optional for Vercel.
# Vercel uses its own mechanism to serve the 'app' object.
# You can keep it for local testing if you wish.
# if __name__ == "__main__":
#     import uvicorn
#     default_port = 8000
#     try:
#         port = int(os.environ.get("PORT", str(default_port)))
#     except ValueError:
#         port = default_port
#     print(f"--- Starting TeleSocial Proxy API (Local Development) ---")
#     print(f"INFO:     Uvicorn running on http://0.0.0.0:{port} (Press CTRL+C to quit)")
#     print(f"INFO:     Access API docs at http://127.0.0.1:{port}/docs")
#     uvicorn.run(app, host="0.0.0.0", port=port)
