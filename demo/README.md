# CodeOracle Demo

## How to Run

### 1. Install Dependencies

```bash
cd codeoracle/backend
pip install -r requirements.txt
```

### 2. Set API Key

```bash
export ANTHROPIC_API_KEY=your_key_here
```

### 3. Start Backend

```bash
uvicorn main:app --reload --port 8000
```

### 4. Open Frontend

Open `frontend/index.html` in your browser (just double-click the file).

## How to Demo

### Option A: Run Demo Analysis
1. Click "Run Demo Analysis" button on the upload screen
2. The demo analyzes `demo/sample_legacy.py` - a Python 2 style inventory management module
3. Wait for the AI to generate explanations, tests, and refactored code

### Option B: Upload Your Own Code
1. Create a ZIP file containing Python files
2. Drag and drop onto the upload zone (or click to select)
3. Wait for analysis to complete

## What the Demo Shows

The `sample_legacy.py` file contains:
- 5+ functions with Python 2 style patterns
- 1 class with 3 methods
- Legacy patterns: bare except, `!= None` checks, `%` string formatting, `print()` calls
- ~80 lines of realistic legacy code

## Project Structure

```
codeoracle/
├── backend/
│   ├── main.py              # FastAPI server
│   ├── parser.py            # Python AST parser
│   ├── llm.py               # Anthropic Claude integration
│   ├── graph.py             # Dependency graph builder
│   ├── coverage_runner.py   # Pytest coverage runner
│   └── requirements.txt
├── frontend/
│   └── index.html           # React app (CDN, no build step)
└── demo/
    ├── sample_legacy.py     # Demo legacy Python file
    └── README.md            # This file
```

## API Endpoints

- `GET /health` - Health check
- `POST /analyze` - Upload ZIP for analysis
- `GET /results/{job_id}` - Get analysis results
- `GET /demo` - Run demo analysis