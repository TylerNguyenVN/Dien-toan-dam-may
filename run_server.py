#!/usr/bin/env python3
"""
run_server.py - Starts the CSV Analytics Dashboard FastAPI service on http://127.0.0.1:8000
"""
import uvicorn

if __name__ == "__main__":
    print("\n=======================================================")
    print("   Starting CSV Analytics Dashboard Server")
    print("   Access Web UI & Dashboard: http://127.0.0.1:8000")
    print("   API Docs (Swagger UI):     http://127.0.0.1:8000/docs")
    print("=======================================================\n")
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000, reload=True)
