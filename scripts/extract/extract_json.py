import json


def extract_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)
