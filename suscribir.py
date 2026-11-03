import requests

WABA_ID = "1147720064438763"
TOKEN = "EAAeiZCeyL9koBSxjslsWvIhL4Y5n0iZBjLiKm4qKPWytcZBZBycv3fMbwnfSxGkTXdkQJVZB6LswjadbOGlv3YSnet6J3QWNX6qxYQEjHiiZA86vVX5saDnDIXC8erW1XiN2I1M1anhV8pfsLflnPWX35L0RdClRNZBPayJAAalO6wdFLeYpyivC2Dcdp8imFZAXHZAbQO4T7YWMssRfXi3H9e5NtwKitUbKz7xofQUSM0ZA3IZC2pCFjjFf39wxZCqFdosZCSZCE57swpnqgcdbOyboSefQZDZD"

url = f"https://graph.facebook.com/v21.0/{WABA_ID}/subscribed_apps"
h = {"Authorization": f"Bearer {TOKEN}"}

print("ANTES:", requests.get(url, headers=h).text)
print("SUSCRIBIR:", requests.post(url, headers=h).text)
print("DESPUÉS:", requests.get(url, headers=h).text)