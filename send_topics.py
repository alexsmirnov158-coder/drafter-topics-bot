#!/usr/bin/env python3
"""
Pick 5 random topics from bank (distinct categories), send to owner via Telegram Bot API.
BOT_TOKEN and CHAT_ID come from environment (set as GitHub Actions secrets).
"""
import os, sys, random, urllib.request, urllib.parse

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
BANK = "topics-bank.tsv"

rows = []
with open(BANK, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        cat, _, title = line.partition("\t")
        if title:
            rows.append((cat.strip(), title.strip()))

random.shuffle(rows)
picked, seen = [], set()
for cat, title in rows:
    if cat not in seen:
        picked.append((cat, title))
        seen.add(cat)
        if len(picked) == 5:
            break
if len(picked) < 5:
    for cat, title in rows:
        if (cat, title) not in picked:
            picked.append((cat, title))
            if len(picked) == 5:
                break

lines = ["☀️ Темы на сегодня:", ""]
for i, (cat, title) in enumerate(picked, 1):
    lines.append(f"{i}. {cat} — {title}")
lines += ["", "Ответь номером или своей идеей."]
text = "\n".join(lines)

data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
req = urllib.request.Request(
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    data=data,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as r:
    body = r.read().decode("utf-8")
    print(body)
    if '"ok":true' not in body:
        sys.exit(1)

# Persist picked topics so we can map "user reply: 3" later
with open("last-topics.txt", "w", encoding="utf-8") as f:
    f.write(text)
