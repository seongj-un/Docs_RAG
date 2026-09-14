"""MCP server exposing this project's retrieval pipeline to agents (M7 W2).

Layout mirrors ``app/routers`` + ``app/services``: ``server.py`` builds the
server the way ``main.py`` builds the FastAPI app, ``tools.py`` registers onto
it the way a router registers routes, ``auth.py`` resolves identity, and
``descriptions.py`` holds the text an agent reads.

Nothing here re-implements retrieval. The tool drives
``services.pipeline.QueryRunner``, the same object the two HTTP query
endpoints drive, so limits, tenant isolation and tracing cannot differ between
a question asked in the web UI and the same question asked by an agent.

앱과 마찬가지로 이 패키지는 아무것도 re-export 하지 않는다 — ``app.routers``
와 같다. ``server.py`` 가 ``tools.py`` 를 가져오므로, 여기서 ``server`` 를
끌어오면 패키지 초기화가 자기 자신을 거쳐 돌아오는 순환이 된다.
"""
