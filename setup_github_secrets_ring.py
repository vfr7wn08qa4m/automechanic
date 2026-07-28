#!/usr/bin/env python3
"""Setup ADO и API секреты на все 8 GitHub аккаунтов."""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# UTF-8 for Windows
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


def gh_api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """GitHub API запрос."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.github.com{path}", data=data, method=method,
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        print(f"  ✗ HTTP {e.code}: {e.read().decode()[:200]}")
        raise


def set_secret(token: str, owner: str, repo: str, secret_name: str, value: str) -> bool:
    """Установить GitHub Actions secret."""
    try:
        # Получить public key
        key_resp = gh_api("GET", f"/repos/{owner}/{repo}/actions/secrets/public-key", token)
        public_key = key_resp.get("key")
        key_id = key_resp.get("key_id")

        # Зашифровать и установить
        import base64
        import nacl.public
        import nacl.encoding

        pk = nacl.public.PublicKey(public_key, encoder=nacl.encoding.Base64Encoder)
        box = nacl.public.SealedBox(pk)
        encrypted = box.encrypt(value.encode())
        encoded_value = base64.b64encode(bytes(encrypted)).decode()

        gh_api("PUT", f"/repos/{owner}/{repo}/actions/secrets/{secret_name}", token, {
            "encrypted_value": encoded_value,
            "key_id": key_id
        })
        print(f"      ✓ {secret_name}")
        return True
    except Exception as e:
        print(f"      ✗ {secret_name}: {str(e)[:80]}")
        return False


def main():
    print("=" * 60)
    print("SETUP GITHUB SECRETS: ADO + API на все 8 аккаунтов")
    print("=" * 60)
    print()

    # Читаю токены
    tokens_file = Path("ghtockens.txt")
    if not tokens_file.exists():
        print("✗ ghtockens.txt не найден")
        return False

    with open(tokens_file) as f:
        tokens = [line.strip() for line in f if line.strip()]

    if len(tokens) != 8:
        print(f"✗ ожидаю 8 токенов, найдено {len(tokens)}")
        return False

    print(f"Загружено {len(tokens)} токенов")
    print()

    # Читаю значения секретов
    secrets = {
        "ADO_ORG": os.getenv("ADO_ORG", "gpsgroupagent12"),
        "ADO_PROJECT": os.getenv("ADO_PROJECT", "AutoMechanic"),
        "ADO_PAT": os.getenv("ADO_PAT", ""),
        "YOUTUBE_API_KEY": os.getenv("YOUTUBE_API_KEY", ""),
        "DISTILL_API_KEY": os.getenv("DISTILL_API_KEY", ""),
        "NIM_API_KEY": os.getenv("NIM_API_KEY", ""),
        "NVIDIA_NIM_API_KEY": os.getenv("NVIDIA_NIM_API_KEY", ""),
        "KIMI_API_KEY": os.getenv("KIMI_API_KEY", ""),
        "QDRANT_URL": os.getenv("QDRANT_URL", ""),
        "QDRANT_API_KEY": os.getenv("QDRANT_API_KEY", ""),
        "BACKUP_REPO": os.getenv("BACKUP_REPO", ""),
        "BACKUP_GITHUB_TOKEN": os.getenv("BACKUP_GITHUB_TOKEN", ""),
    }

    print("Секреты для установки:")
    for k, v in secrets.items():
        status = "✓" if v else "✗ ПУСТО"
        val_display = (v[:20] + "...") if v else "(не установлен)"
        print(f"  {status} {k}: {val_display}")
    print()

    missing = [k for k, v in secrets.items() if not v]
    if missing:
        print(f"⚠️  Отсутствуют: {', '.join(missing)}")
        print("   (установите в окружении или передайте через аргументы)")
        print()

    # Установить на каждый аккаунт
    accounts = []
    print("Определяю аккаунты...")
    for i, token in enumerate(tokens, 1):
        try:
            user_info = gh_api("GET", "/user", token)
            login = user_info.get("login", f"account-{i}")
            accounts.append((login, token))
            print(f"  {i}. {login}")
        except Exception as e:
            print(f"  ✗ {i}: {str(e)[:60]}")
            return False

    print()
    print("Устанавливаю секреты на каждый аккаунт...")
    print()

    for login, token in accounts:
        print(f"[{login}]")
        repo = "automechanic"
        for secret_name, value in secrets.items():
            if not value:
                print(f"      ⊘ {secret_name} (не установлен, пропускаю)")
                continue
            set_secret(token, login, repo, secret_name, value)

    print()
    print("=" * 60)
    print("✓ Секреты установлены!")
    print("=" * 60)

    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
