import os
from dotenv import load_dotenv
from google import genai

load_dotenv('D:/hackorbit/CodeOrbit/backend/.env')
api_key = os.environ.get('GEMINI_API_KEY')

key_valid = bool(api_key and api_key != 'your_key_here')
print(f'API Key loaded: {key_valid}')

if key_valid:
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents='Say hello in one word'
    )
    print(f'API Response: {response.text}')
    print('SUCCESS: Gemini API is working!')
else:
    print('ERROR: Please set your actual GEMINI_API_KEY in .env file')
