#!/usr/bin/env python3
"""
Health check script para Docker HEALTHCHECK y Fly.io.

Retorna exit code 0 si el bot está sano, 1 si no lo está.
Se ejecuta desde el Dockerfile como health check.
"""
import sys
import urllib.request
import urllib.error


def check() -> bool:
    try:
        with urllib.request.urlopen(
            "http://localhost:8080/health", timeout=5
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, ConnectionRefusedError):
        return False


if __name__ == "__main__":
    ok = check()
    print("OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)
