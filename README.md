# Candidate kit

1. Setup the `Mongo DB` database via `Atals (Free Tier - M0)` and set the *admin* and *password*.
2. Setup a Vitual Local Environment using command `python -m venv .venv`
3. Activate the Virtual Environment with command `.venv/bin/activate`
4. Install all requirements mentioned in the `requirements.txt` via command `pip install -r requirements.txt`
5. Create environment variable`.env` file and set variable `MONGO_URI`and `MONGO_DB` with respective *URI for Cluster Connection* and *Database* name.
6. Load sample data (`python sample_seed.py`) *[Only if needed]*.
7. Run the app using *uvicorn* with command `uvicorn app.main:app --port 8000 --reload`.