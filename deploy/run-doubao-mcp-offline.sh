#!/bin/sh
set -eu

# DoubaoMcpClient normally passes uvx resolution arguments here.  The Ningxia
# host cannot reach GitHub, so this deployment entrypoint intentionally ignores
# those arguments and runs the already hydrated, revision-pinned tool directly.
exec /home/ec2-user/tavily/.doubao-mcp/bin/mcp-server-askecho-search-infinity
