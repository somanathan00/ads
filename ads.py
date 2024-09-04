import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
import stripe
from flask import Flask, request, jsonify
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
import threading
import time
import logging

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),  # Log to console
        logging.FileHandler("app.log")  # Log to a file named app.log
    ]
)

logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Set Stripe API key
stripe.api_key = os.getenv("STRIPE_API_KEY")
endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")

# Email configuration
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Initialize Firebase Admin SDK
try:
    firebase_admin.get_app()  # Check if app is already initialized
except ValueError:
    cred = credentials.Certificate(os.getenv("FIREBASE_CREDENTIALS_PATH"))
    firebase_admin.initialize_app(cred)

db = firestore.client()
@app.route('/')
def index():
    return "Welcome to the Ads Service!", 200

def send_payment_link_to_admin(ad_admin_email, payment_link,ad_title):
    logger.info(f"Sending payment link to {ad_admin_email}")
    message = MIMEMultipart("alternative")
    message["Subject"] = "Action Required: Payment for Ad Approval"
    message["From"] = SMTP_USERNAME
    message["To"] = ad_admin_email

    text = f"""\
    Dear Admin,

    We hope this message finds you well.

    We are writing to inform you that the ad with the title "{ad_title}" from MRL has expired. To continue showcasing your ad, we kindly request that you complete the payment for its renewal and approval.

    Please use the following link to make the payment:

    {payment_link}

    Once the payment is completed, your ad will be reviewed and approved for continued display.

    If you have any questions or need further assistance, feel free to contact us.

    Thank you for your prompt attention to this matter.

    Best regards,
    The MRL Team
    """

    html = f"""\
    <!DOCTYPE html>
    <html>
    <head>
        <style>
        body {{ font-family: Arial, sans-serif; }}
        .container {{ max-width: 600px; margin: auto; padding: 20px; }}
        .button {{ display: inline-block; padding: 10px 20px; font-size: 16px; color: #fff; background-color: #007BFF; text-decoration: none; border-radius: 5px; }}
        .footer {{ margin-top: 20px; font-size: 14px; color: #555; }}
        </style>
    </head>
    <body>
        <div class="container">
        <p>Dear Admin,</p>
        <p>We hope this message finds you well.</p>
        <p>We are writing to inform you that the ad with the title "<strong>{ad_title}</strong>" from MRL has expired. To continue showcasing your ad, we kindly request that you complete the payment for its renewal and approval.</p>
        <p>Please use the following link to make the payment:</p>
        <p><a href="{payment_link}" class="button">Pay Now</a></p>
        <p>Once the payment is completed, your ad will be reviewed and approved for continued display.</p>
        <p>If you have any questions or need further assistance, feel free to contact us.</p>
        <p class="footer">Thank you for your prompt attention to this matter.</p>
        <p class="footer">Best regards,<br>The MRL Team</p>
        </div>
    </body>
    </html>
    """

    part1 = MIMEText(text, "plain")
    part2 = MIMEText(html, "html")

    message.attach(part1)
    message.attach(part2)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, ad_admin_email, message.as_string())
        logger.info(f"Payment link sent successfully to {ad_admin_email}")
    except Exception as e:
        logger.error(f"Failed to send payment link to {ad_admin_email}: {e}")

def create_payment_link(ad_title, ad_id):
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f"Payment for ad: {ad_title}",
                    },
                    'unit_amount': 1000,  # Amount in cents
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url='https://example.com/success',  # Replace with your success URL
            cancel_url='https://example.com/cancel',  # Replace with your cancel URL
            metadata={
                'ad_title': ad_title,
                'ad_id': ad_id,
            }
        )
        logger.info(f"Payment link created successfully for ad {ad_title}")
        return session.url
    except Exception as e:
        logger.error(f"Failed to create payment link for ad {ad_title}: {e}")
        return None

def check_ads_periodically():
    while True:
        logger.info("Checking ads periodically...")
        now = datetime.now(timezone.utc)
        ads_ref = db.collection('customads')
        query = ads_ref.where('isApproved', '==', False).get()

        for doc in query:
            ad_data = doc.to_dict()
            ad_title = ad_data.get('title', 'Unknown')
            ad_admin_email = ad_data.get('ad_admin', None)
            ad_id = ad_data.get('adUnitId', None)
            last_email_sent = ad_data.get('last_email_sent', None)

            if not ad_id or not ad_admin_email:
                logger.warning(f"Missing adUnitId or ad_admin for ad: {ad_title}")
                continue

            if last_email_sent:
                last_email_sent = last_email_sent.replace(tzinfo=timezone.utc)
                if now - last_email_sent < timedelta(days=1):
                    logger.info(f"Email already sent recently for ad: {ad_title}")
                    continue

            payment_link = create_payment_link(ad_title, ad_id)
            if payment_link:
                send_payment_link_to_admin(ad_admin_email, payment_link,ad_title)
                logger.info(f"Payment link sent to {ad_admin_email} for ad {ad_title}")
                ads_ref.document(doc.id).update({
                    'last_email_sent': now
                })
            else:
                logger.error(f"Failed to create or send payment link for ad {ad_title}")

        time.sleep(300)  # Sleep for 5 minutes

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    logger.info("Received Stripe webhook")
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
        logger.info(f"Webhook event type: {event['type']}")
    except ValueError as e:
        logger.error("Invalid payload")
        return jsonify(success=False), 400
    except stripe.error.SignatureVerificationError as e:
        logger.error("Invalid signature")
        return jsonify(success=False), 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        ad_id = session['metadata'].get('ad_id', 'Unknown')

        ads_ref = db.collection('customads')
        query = ads_ref.where('adUnitId', '==', ad_id).get()

        for doc in query:
            doc_ref = ads_ref.document(doc.id)
            doc_ref.update({
                'isApproved': True,
                'status': 'active'
            })
            logger.info(f"Ad {ad_id} has been approved and activated.")

    return jsonify(success=True)

if __name__ == '__main__':
    logger.info("Starting Flask app and periodic ad checker thread...")
    thread = threading.Thread(target=check_ads_periodically)
    thread.start()
    
    app.run(host='0.0.0.0', port=8080)