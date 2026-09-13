~/PatchPilot/verify_pipeline.py
# Execute within (~/PatchPilot) virtualenv
pip install fastapi uvicorn pydantic
pip freeze > requirements.txt 

bash# 1. Navigate to your project directory
cd ~/PatchPilot

# 2. Activate your virtual environment (if not already active)
source .venv/bin/activate

# 3. Install FastAPI and Uvicorn packages explicitly
pip install fastapi uvicorn pydantic

# 4. Export the complete, updated dependency tree to requirements.txt
pip freeze > requirements.txt