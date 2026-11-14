"""Sign test clients in, so API tests run as a real account."""
FAST_HASH = "scrypt:1024:8:1"  # Tests only. The app's default is OWASP's scrypt minimum (N=2^17).
USERNAME, PASSWORD = "tester", "a long enough test passphrase"


def signed_in(client, username=USERNAME, password=PASSWORD):
    """Sign in, creating the account the first time. Returns the client for chaining."""
    client.application.config["PASSWORD_HASH_METHOD"] = FAST_HASH
    credentials = {"username": username, "password": password}
    if client.post("/api/auth/login", json=credentials).status_code != 200:
        created = client.post("/api/auth/register", json={**credentials, "name": "Ananth", "pronouns": "he/him"})
        assert created.status_code == 201, created.get_json()
    return client
