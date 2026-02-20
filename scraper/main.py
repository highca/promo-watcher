name: O-Lens Event Monitor

on:
  schedule:
    - cron: "*/15 * * * *"   # 15분마다 (UTC 기준)
  workflow_dispatch:

permissions:
  contents: write   # state 파일 자동 커밋을 위해 필요

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Python deps
        run: |
          pip install -U pip
          pip install playwright requests

      - name: Install Chromium for Playwright
        run: |
          python -m playwright install --with-deps chromium

      - name: Run scraper
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
        run: |
          python scraper/main.py

      - name: Commit state if changed
        run: |
          if [[ -n "$(git status --porcelain)" ]]; then
            git config user.name "github-actions[bot]"
            git config user.email "github-actions[bot]@users.noreply.github.com"
            git add -A
            git commit -m "Update olens state"
            git push
          fi
