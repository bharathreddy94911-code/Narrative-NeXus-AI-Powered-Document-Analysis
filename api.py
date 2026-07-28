import logging
import torch
import PyPDF2
import docx
import numpy as np
import re
from collections import defaultdict
from flask import Flask, request, jsonify
from flask_cors import CORS
from transformers import LEDForConditionalGeneration, LEDTokenizer, pipeline
import nltk
from nltk.tokenize import sent_tokenize, word_tokenize
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from sklearn.decomposition import LatentDirichletAllocation, NMF
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from werkzeug.security import generate_password_hash, check_password_hash

# Import database functions from our new database file
import database

# Download NLTK data if not present
try:
    nltk.data.find('tokenizers/punkt')
except nltk.downloader.DownloadError:
    nltk.download('punkt')

# --- CONFIGURATION ---
SUMMARIZER_MODEL_PATH = "allenai/led-base-16384"
NER_MODEL_NAME = "dbmdz/bert-large-cased-finetuned-conll03-english"
MAX_INPUT_LENGTH = 16384  # Max length for LED model
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)
CORS(app)

# --- DATABASE INITIALIZATION ---
# This will create the database file and table when the app starts
database.init_db()

# --- MODEL LOADING ---
device = "cuda" if torch.cuda.is_available() else "cpu"
logging.info(f"Using device: {device}")

summarization_tokenizer = None
summarization_model = None
ner_pipeline = None
vader_analyzer = SentimentIntensityAnalyzer()


def load_models():
    global summarization_tokenizer, summarization_model, ner_pipeline
    logging.info("Loading AI models...")
    try:
        summarization_tokenizer = LEDTokenizer.from_pretrained(SUMMARIZER_MODEL_PATH)
        summarization_model = LEDForConditionalGeneration.from_pretrained(SUMMARIZER_MODEL_PATH).to(device)
        logging.info("Summarization model loaded successfully.")
    except Exception as e:
        logging.error(f"Error loading summarization model: {e}. Summarization will be disabled.")

    try:
        ner_pipeline = pipeline("ner", model=NER_MODEL_NAME, aggregation_strategy="simple",
                                device=0 if device == "cuda" else -1)
        logging.info("NER model loaded successfully.")
    except Exception as e:
        logging.warning(f"Could not load NER model: {e}. Insight generation will be disabled.")


# --- HELPER & ANALYSIS FUNCTIONS ---
def extract_text_from_file(file_storage):
    filename = file_storage.filename.lower()
    text = ""
    try:
        if filename.endswith('.pdf'):
            pdf_reader = PyPDF2.PdfReader(file_storage.stream)
            text = "".join(page.extract_text() for page in pdf_reader.pages if page.extract_text())
        elif filename.endswith('.docx'):
            doc = docx.Document(file_storage.stream)
            text = "\n".join(para.text for para in doc.paragraphs if para.text)
        else:
            text = file_storage.stream.read().decode('utf-8', errors='ignore')
    except Exception as e:
        logging.error(f"Error extracting text from {filename}: {e}")
    return text


def clean_text(text):
    text = re.sub(r'http\S+', '', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^a-zA-Z0-9\s.,-]', '', text, re.I | re.A)
    return text.strip()


def perform_summarization(text):
    if not summarization_model or not summarization_tokenizer:
        return {"error": "Summarization model not available."}
    try:
        inputs = summarization_tokenizer(text, return_tensors="pt", max_length=MAX_INPUT_LENGTH, truncation=True).to(
            device)
        summary_ids = summarization_model.generate(inputs.input_ids, num_beams=4, max_length=256, early_stopping=True)
        summary = summarization_tokenizer.decode(summary_ids[0], skip_special_tokens=True)
        return {"summary": summary}
    except Exception as e:
        logging.error(f"Summarization failed: {e}")
        return {"error": str(e)}


def perform_topic_modeling(text, model_choice, num_topics):
    try:
        sentences = sent_tokenize(text)
        if len(sentences) < num_topics:
            return {"error": "Not enough text to perform topic modeling. Try fewer topics or a larger document."}

        vectorizer = TfidfVectorizer(max_df=0.95, min_df=2, stop_words='english')
        tfidf = vectorizer.fit_transform(sentences)

        if model_choice == 'LDA':
            model = LatentDirichletAllocation(n_components=num_topics, random_state=42)
        else:  # NMF
            model = NMF(n_components=num_topics, random_state=42)

        topic_document_matrix = model.fit_transform(tfidf)
        feature_names = vectorizer.get_feature_names_out()

        results = {"topics": [], "topic_distribution": [], "overall_sentiment": {}}

        # Topic Details
        for topic_idx, topic in enumerate(model.components_):
            top_words = [feature_names[i] for i in topic.argsort()[:-10 - 1:-1]]
            topic_sentences_indices = topic_document_matrix[:, topic_idx].argsort()[-3:]
            example_sentences = [{"text": sentences[i], "score": round(float(topic_document_matrix[i, topic_idx]), 4)}
                                 for i in topic_sentences_indices]

            topic_text = " ".join([s['text'] for s in example_sentences])
            sentiment_score = vader_analyzer.polarity_scores(topic_text)['compound']
            sentiment_label = 'Positive' if sentiment_score >= 0.05 else 'Negative' if sentiment_score <= -0.05 else 'Neutral'

            results["topics"].append({
                "topic_id": topic_idx + 1,
                "top_words": top_words,
                "example_sentences": example_sentences,
                "sentiment": {"label": sentiment_label, "score": sentiment_score}
            })

        # Document-level analysis
        doc_lengths = [len(word_tokenize(s)) for s in sentences]
        topic_word_counts = np.dot(topic_document_matrix.T, doc_lengths)
        total_word_count = sum(doc_lengths)
        results["topic_distribution"] = [{"topic_id": i + 1, "percentage": round((count / total_word_count) * 100, 2)}
                                         for i, count in enumerate(topic_word_counts)]

        overall_sentiment_score = vader_analyzer.polarity_scores(text)['compound']
        overall_sentiment_label = 'Positive' if overall_sentiment_score >= 0.05 else 'Negative' if overall_sentiment_score <= -0.05 else 'Neutral'
        results["overall_sentiment"] = {"sentiment": overall_sentiment_label,
                                        "confidence": abs(overall_sentiment_score)}

        return results
    except Exception as e:
        logging.error(f"Topic modeling failed: {e}")
        return {"error": str(e)}


def perform_insights(text):
    if not ner_pipeline:
        return {"error": "NER model for insights is not available."}
    try:
        entities = ner_pipeline(text)
        insights = defaultdict(list)
        entity_counts = defaultdict(int)

        for entity in entities:
            # Shorten entity type for clarity (e.g., 'B-PER' -> 'PERSON')
            entity_type = entity['entity_group']
            insights[entity_type].append(entity['word'])

        # Deduplicate and count
        for entity_type, entity_list in insights.items():
            unique_entities = sorted(list(set(entity_list)))
            insights[entity_type] = unique_entities
            entity_counts[entity_type] = len(unique_entities)

        return {"insights": dict(insights), "entity_counts": dict(entity_counts)}
    except Exception as e:
        logging.error(f"Insight extraction failed: {e}")
        return {"error": str(e)}


# --- NEW AUTHENTICATION ROUTES ---
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"error": "Username and password are required."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters long."}), 400

    try:
        # Check if user already exists
        if database.get_user(username):
            return jsonify({"error": "Username already exists."}), 409

        # Hash the password and add the new user
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        database.add_user(username, hashed_password)
        return jsonify({"message": f"User '{username}' registered successfully!"}), 201
    except Exception as e:
        logging.error(f"Registration error for {username}: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"error": "Username and password are required."}), 400

    user = database.get_user(username)
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({"error": "Invalid username or password."}), 401

    # In a real app, you would generate a session token (JWT) here
    return jsonify({"message": "Login successful!", "username": user['username']}), 200


# --- CORE ANALYSIS ROUTE ---
@app.route('/upload-and-analyze', methods=['POST'])
def upload_and_analyze():
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({"error": "No file selected."}), 400

    file = request.files['file']
    analysis_type = request.form.get('analysis_type')

    raw_text = extract_text_from_file(file)
    if not raw_text:
        return jsonify({"error": "Could not extract text from the file."}), 400

    clean_document_text = clean_text(raw_text)

    if analysis_type == 'topic_modeling':
        model_choice = request.form.get('model', 'LDA')
        num_topics = int(request.form.get('num_topics', 10))
        results = perform_topic_modeling(clean_document_text, model_choice, num_topics)
    elif analysis_type == 'summarization':
        results = perform_summarization(clean_document_text)
    elif analysis_type == 'insights':
        results = perform_insights(clean_document_text)
    else:
        return jsonify({"error": "Invalid analysis type."}), 400

    return jsonify(results)


if __name__ == '__main__':
    load_models()  # Load models once on startup
    app.run(host='0.0.0.0', port=5001, debug=True)
