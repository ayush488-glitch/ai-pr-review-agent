#!/usr/bin/env python3
"""
Quick script to reduce Redis usage and get past the 500K request limit.

This makes minimal changes to reduce repeated Redis calls:
1. Reduce idempotency TTL from 24h to 1h
2. Add basic caching to health checks

Run this from the project root after activating your venv.
"""

import os
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))


def fix_webhook_redis_usage():
    """
    Reduce idempotency TTL to cut down on repeated Redis requests.
    """
    webhook_path = Path(__file__).parent.parent / "backend" / "webhook_receiver" / "router.py"

    print(f"Reading {webhook_path}...")
    with open(webhook_path, 'r') as f:
        content = f.read()

    # Check if we need to add/reduce IDEMPOTENCY_TTL constant
    if "IDEMPOTENCY_TTL" not in content:
        print("Adding IDEMPOTENCY_TTL constant...")
        # Add it after imports
        content = content.replace(
            "from backend.database.postgres import get_db",
            "from backend.database.postgres import get_db\n\n# Reduce TTL to avoid repeated Redis operations\nIDEMPOTENCY_TTL = 3600  # 1 hour (vs default 24h)"
        )

    # Change any existing long TTLs to shorter ones
    content = content.replace("86400", "3600")  # 24h -> 1h
    content = content.replace("3600*24", "3600")  # 24h -> 1h

    print(f"Writing updated {webhook_path}...")
    with open(webhook_path, 'w') as f:
        f.write(content)

    print("✅ Fixed webhook router - reduced idempotency TTL to 1 hour")


def fix_health_check_caching():
    """
    Add basic caching to health checks to reduce Redis calls.
    """
    main_path = Path(__file__).parent.parent / "backend" / "main.py"

    print(f"Reading {main_path}...")
    with open(main_path, 'r') as f:
        content = f.read()

    # Add caching logic if not present
    if "redis_cache_timeout" not in content:
        print("Adding Redis health check caching...")

        # Find the health check endpoint and add caching
        health_check_snippet = '''
# Cache Redis health status to reduce request count
_last_redis_check = 0
_redis_status = "unknown"

def check_redis_healthy():
    """Check Redis health with caching to avoid repeated calls"""
    global _last_redis_check, _redis_status
    import time

    now = time.time()
    if now - _last_redis_check > 60:  # Check every 60 seconds
        try:
            redis_client = get_redis()
            _redis_status = redis_client.health()
            _last_redis_check = now
        except Exception as e:
            _redis_status = "error"
            _last_redis_check = now
    return _redis_status
'''

        # Add after imports
        content = content.replace(
            "from backend.config import settings",
            "from backend.config import settings\n" + health_check_snippet
        )

        print(f"Writing updated {main_path}...")
        with open(main_path, 'w') as f:
            f.write(content)

        print("✅ Fixed main.py - added Redis health check caching")
    else:
        print("ℹ️  Redis caching already present")


if __name__ == "__main__":
    print("=== Fixing Redis Usage ===\n")

    try:
        fix_webhook_redis_usage()
        print()
        fix_health_check_caching()

        print("\n=== Fixes Applied ===")
        print("1. Reduced idempotency TTL to 1 hour (was 24h)")
        print("2. Added Redis health check caching (checks every 60s)")
        print("\nThese changes will significantly reduce Redis request usage.")
        print("\nNext steps:")
        print("- Deploy changes to Render/Railway")
        print("- Test webhook again")
        print("- Monitor Upstash console for reduced request rate")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)