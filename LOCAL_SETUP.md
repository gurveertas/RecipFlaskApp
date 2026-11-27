# RECIP - Local Development

## Requirements

- Python 3.12 recommended
- Git
- Access to the private RECIP development repository

## Clone the repository

git clone https://github.com/rededucation20/recip-development.git
cd recip-development

## Create a Python virtual environment

python3 -m venv .venv

macOS/Linux:
source .venv/bin/activate

Windows PowerShell:
.venv\Scripts\Activate.ps1

## Install dependencies

python -m pip install -r requirements.txt

## Configuration

Copy .env.example to .env.

Leave API credentials empty for demo mode.
Use only approved development API credentials.
Production credentials must not be used.

Use a local SQLite database, separate from production.

## Start the application

python -m flask --app webapp run --host 127.0.0.1 --port 5000

Open http://127.0.0.1:5000

Article Studio:
http://127.0.0.1:5000/studio

## Development guidelines

- Create a separate branch for development.
- Submit changes through pull requests.
- Do not commit credentials or local databases.
- Do not connect to production WordPress.
- Do not share the source code outside authorised personnel.
- Production integration requires internal approval.

