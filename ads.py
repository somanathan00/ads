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

# Load environment variables
load_dotenv()

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

def send_payment_link_to_admin(ad_admin_email, payment_link):
    message = MIMEMultipart("alternative")
    message["Subject"] = "Payment Required for Ad Approval"
    message["From"] = SMTP_USERNAME
    message["To"] = ad_admin_email
    
    text = f"Please complete the payment using the following link to approve your ad:\n{payment_link}"
    html = f"<p>Please complete the payment using the following link to approve your ad:</p><a href='{payment_link}'>Pay Now</a>"

    part1 = MIMEText(text, "plain")
    part2 = MIMEText(html, "html")

    message.attach(part1)
    message.attach(part2)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_USERNAME, ad_admin_email, message.as_string())

def create_payment_link(ad_title, ad_id):
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
    return session.url

def check_ads_periodically():
    while True:
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
                print(f"Missing adUnitId or ad_admin for ad: {ad_title}")
                continue

            # Check if an email was sent within the last 24 hours
            if last_email_sent:
                last_email_sent = last_email_sent.replace(tzinfo=timezone.utc)
                if now - last_email_sent < timedelta(days=1):
                    print(f"Email already sent recently for ad: {ad_title}")
                    continue

            # Create payment link and send email
            payment_link = create_payment_link(ad_title, ad_id)
            send_payment_link_to_admin(ad_admin_email, payment_link)
            print(f"Payment link sent to {ad_admin_email} for ad {ad_title}")

            # Update Firestore document with the new last_email_sent timestamp
            ads_ref.document(doc.id).update({
                'last_email_sent': now
            })

        time.sleep(300)  # Sleep for 24 hours

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError as e:
        print("Invalid payload")
        return jsonify(success=False), 400
    except stripe.error.SignatureVerificationError as e:
        print("Invalid signature")
        return jsonify(success=False), 400

    print("Event received:", event)

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
            print(f"Ad {ad_id} has been approved and activated.")

    return jsonify(success=True)

if __name__ == '__main__':
    # Start the periodic check in a separate thread
    thread = threading.Thread(target=check_ads_periodically)
    thread.start()
    
    app.run(host='0.0.0.0', port=8080)



