#!/bin/bash

# Load environment variables...
if [ -f ./.env ]; then
    . ./.env
elif [ -f ./local.env ]; then
    . ./local.env
else
    echo ".env file not found, proceeding without loading environment variables."
fi

# Set PYTHONPATH to the current directory
export PYTHONPATH=$(pwd)

# Set reload flag depending on ENV variable
if [ "$ENV" = "prod" ]; then
    RELOAD=""
     uv run uvicorn app.__main__:app --host "0.0.0.0" --port "$PORT"
else
    uv run uvicorn app.__main__:app --host "127.0.0.1" --port "$PORT" --reload  
fi