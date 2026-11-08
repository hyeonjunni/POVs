import argparse
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles


def create_app(directory: Path):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hop_headers = {"host", "connection", "keep-alive", "transfer-encoding", "content-length",
                   "content-encoding", "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer"}

    @app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def proxy(path: str, request: Request):
        headers = {key: value for key, value in request.headers.items() if key.lower() not in hop_headers}
        headers["host"] = request.url.netloc
        try:
            async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
                upstream = await client.request(request.method, "http://127.0.0.1:8002/api/v1/" + path,
                    params=request.query_params, headers=headers, content=await request.body())
            return Response(upstream.content, status_code=upstream.status_code,
                headers={key: value for key, value in upstream.headers.items() if key.lower() not in hop_headers})
        except httpx.HTTPError:
            return Response("Backend unavailable", status_code=503)

    app.mount("/", StaticFiles(directory=directory, html=True), name="web")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args()
    uvicorn.run(create_app(args.directory), host=args.host, port=args.port)
