# Docket

Write the decision, the road you did not take, and the date you will open it again.

Live: https://docket.holyx.cc
Source: https://github.com/mk7luke/docket

The owner link edits. The share link reads. There is no account.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8886
```

Health: `GET /health`

Built on holy-hermes.
