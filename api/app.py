import os
import json
import hashlib
import io
import re
from datetime import datetime, timedelta
from collections import defaultdict
import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import PyPDF2

# --- Configuration ---
load_dotenv()
# REMOVED: API_KEY = os.getenv("GEMINI_API_KEY") - This will now be passed per request.
OUTPUT_JSON_PATH = "bank_statements_data.json"

# --- Flask App Initialization ---
app = Flask(__name__)
CORS(app)

# --- Enhanced Financial Analysis Functions ---
# Note: These helper functions do not need modification as they don't directly call the API.
def calculate_comprehensive_metrics(financial_data):
    """
    Calculates comprehensive financial metrics from all available data.
    """
    if not financial_data:
        return {}
    
    # Separate document types
    monthly_statements = [d for d in financial_data if d.get('document_type') == 'monthly_statement']
    transaction_lists = [d for d in financial_data if d.get('document_type') == 'transaction_list']
    
    # Sort by date
    monthly_statements.sort(key=lambda x: x.get('statement_period', {}).get('end_date', ''))
    transaction_lists.sort(key=lambda x: x.get('statement_period', {}).get('end_date', ''))
    
    metrics = {}
    
    # Current Net Worth (Latest Balance)
    current_net_worth = 0
    latest_balance_date = None
    
    # Try to get from most recent monthly statement first
    if monthly_statements:
        latest_statement = monthly_statements[-1]
        current_net_worth = latest_statement.get('summary', {}).get('closing_balance', 0)
        latest_balance_date = latest_statement.get('statement_period', {}).get('end_date')
    
    # If we have more recent transaction lists, use those
    if transaction_lists:
        latest_transaction_list = transaction_lists[-1]
        latest_closing_balance = latest_transaction_list.get('summary', {}).get('closing_balance')
        if latest_closing_balance is not None:
            latest_list_date = latest_transaction_list.get('statement_period', {}).get('end_date')
            if not latest_balance_date or (latest_list_date and latest_list_date > latest_balance_date):
                current_net_worth = latest_closing_balance
                latest_balance_date = latest_list_date
    
    metrics['current_net_worth'] = current_net_worth
    metrics['net_worth_as_of_date'] = latest_balance_date
    
    # Historical balances for trend analysis
    balance_history = []
    for stmt in monthly_statements:
        end_date = stmt.get('statement_period', {}).get('end_date')
        closing_balance = stmt.get('summary', {}).get('closing_balance')
        if end_date and closing_balance is not None:
            balance_history.append({
                'date': end_date,
                'balance': closing_balance,
                'source': 'monthly_statement'
            })
    
    # Add transaction list balances if they're more recent
    for tlist in transaction_lists:
        end_date = tlist.get('statement_period', {}).get('end_date')
        closing_balance = tlist.get('summary', {}).get('closing_balance')
        if end_date and closing_balance is not None:
            balance_history.append({
                'date': end_date,
                'balance': closing_balance,
                'source': 'transaction_list'
            })
    
    # Sort and deduplicate balance history (handle None dates)
    balance_history.sort(key=lambda x: x.get('date') or '1900-01-01')
    metrics['balance_history'] = balance_history
    
    # Net worth change calculations (handle None dates)
    if len(balance_history) >= 2:
        first_balance = balance_history[0]['balance']
        latest_balance = balance_history[-1]['balance']
        
        metrics['total_net_worth_change'] = latest_balance - first_balance
        metrics['net_worth_change_percentage'] = ((latest_balance - first_balance) / first_balance * 100) if first_balance != 0 else 0
        
        # Calculate period - handle None dates
        first_date_str = balance_history[0].get('date')
        latest_date_str = balance_history[-1].get('date')
        
        if first_date_str and latest_date_str:
            try:
                first_date = datetime.strptime(first_date_str, '%Y-%m-%d')
                latest_date = datetime.strptime(latest_date_str, '%Y-%m-%d')
                metrics['tracking_period_days'] = (latest_date - first_date).days
                metrics['tracking_period_months'] = metrics['tracking_period_days'] / 30.44
            except ValueError:
                metrics['tracking_period_days'] = 0
                metrics['tracking_period_months'] = 0
    
    # All transactions analysis
    all_transactions = []
    for doc in financial_data:
        all_transactions.extend(doc.get('transactions', []))
    
    # Sort transactions by date (handle None values)
    all_transactions.sort(key=lambda x: x.get('transaction_date') or '1900-01-01')
    
    # Total income and expenses
    total_income = sum(t.get('credit', 0) or 0 for t in all_transactions)
    total_expenses = sum(t.get('debit', 0) or 0 for t in all_transactions)
    
    metrics['total_income_all_time'] = total_income
    metrics['total_expenses_all_time'] = total_expenses
    metrics['net_cash_flow_all_time'] = total_income - total_expenses
    
    # Monthly analysis - handle None dates
    monthly_data = defaultdict(lambda: {'income': 0, 'expenses': 0, 'transactions': []})
    
    for transaction in all_transactions:
        trans_date = transaction.get('transaction_date')
        if trans_date and isinstance(trans_date, str) and len(trans_date) >= 7:
            try:
                month_key = trans_date[:7]  # YYYY-MM format
                monthly_data[month_key]['transactions'].append(transaction)
                if transaction.get('credit'):
                    monthly_data[month_key]['income'] += transaction['credit']
                if transaction.get('debit'):
                    monthly_data[month_key]['expenses'] += transaction['debit']
            except (ValueError, TypeError):
                continue
    
    # Convert to list and sort
    monthly_summary = []
    for month, data in monthly_data.items():
        monthly_summary.append({
            'month': month,
            'income': data['income'],
            'expenses': data['expenses'],
            'net_flow': data['income'] - data['expenses'],
            'transaction_count': len(data['transactions'])
        })
    
    monthly_summary.sort(key=lambda x: x['month'])
    metrics['monthly_summary'] = monthly_summary
    
    # Recent period analysis (last 3 months)
    if monthly_summary:
        recent_months = monthly_summary[-3:]
        recent_income = sum(m['income'] for m in recent_months)
        recent_expenses = sum(m['expenses'] for m in recent_months)
        
        metrics['recent_3_months'] = {
            'income': recent_income,
            'expenses': recent_expenses,
            'net_flow': recent_income - recent_expenses,
            'avg_monthly_income': recent_income / len(recent_months),
            'avg_monthly_expenses': recent_expenses / len(recent_months)
        }
    
    # Income stability analysis
    if len(monthly_summary) >= 3:
        incomes = [m['income'] for m in monthly_summary if m['income'] > 0]
        if incomes:
            avg_income = sum(incomes) / len(incomes)
            income_variance = sum((x - avg_income) ** 2 for x in incomes) / len(incomes)
            income_std_dev = income_variance ** 0.5
            income_stability = max(0, 100 - (income_std_dev / avg_income * 100)) if avg_income > 0 else 0
            
            metrics['income_analysis'] = {
                'average_monthly_income': avg_income,
                'income_stability_score': income_stability,
                'income_volatility': (income_std_dev / avg_income * 100) if avg_income > 0 else 0
            }
    
    # Expense analysis
    expense_categories = defaultdict(lambda: {'total': 0, 'count': 0, 'transactions': []})
    
    for transaction in all_transactions:
        if transaction.get('debit'):
            description = transaction.get('description', '').upper()
            
            # Categorize expenses based on description keywords
            category = 'OTHER'
            if any(word in description for word in ['INWI', 'IAM', 'ORANGE']):
                category = 'TELECOMMUNICATIONS'
            elif any(word in description for word in ['GAB', 'RETRAIT', 'ATM']):
                category = 'CASH_WITHDRAWALS'
            elif any(word in description for word in ['VIREMENT', 'TRANSFER']):
                category = 'TRANSFERS'
            elif any(word in description for word in ['COMMISSION', 'FRAIS', 'TIMBRE']):
                category = 'BANK_FEES'
            elif any(word in description for word in ['PAIEMENT', 'CB']):
                category = 'CARD_PAYMENTS'
            
            expense_categories[category]['total'] += transaction['debit']
            expense_categories[category]['count'] += 1
            expense_categories[category]['transactions'].append(transaction)
    
    metrics['expense_categories'] = dict(expense_categories)
    
    # Spending patterns
    largest_expenses = sorted([t for t in all_transactions if t.get('debit')], 
                            key=lambda x: x['debit'], reverse=True)[:10]
    metrics['largest_expenses'] = largest_expenses
    
    # Recurring transactions analysis - handle None dates
    recurring_patterns = defaultdict(lambda: {'count': 0, 'total_amount': 0, 'avg_amount': 0, 'dates': []})
    
    for transaction in all_transactions:
        if transaction.get('debit'):
            # Normalize description for pattern matching
            desc = transaction.get('description', '')
            if desc:
                desc = re.sub(r'\d{2}/\d{2}(/\d{4})?', '', desc)
                desc = re.sub(r'\d{2}H\d{2}', '', desc)
                desc = re.sub(r'\s+', ' ', desc).strip().upper()
                
                if len(desc) > 5:  # Only consider meaningful descriptions
                    recurring_patterns[desc]['count'] += 1
                    recurring_patterns[desc]['total_amount'] += transaction['debit']
                    trans_date = transaction.get('transaction_date')
                    if trans_date:
                        recurring_patterns[desc]['dates'].append(trans_date)
    
    # Filter for truly recurring (3+ occurrences)
    recurring_expenses = {}
    for pattern, data in recurring_patterns.items():
        if data['count'] >= 3:
            data['avg_amount'] = data['total_amount'] / data['count']
            recurring_expenses[pattern] = data
    
    metrics['recurring_expenses'] = recurring_expenses
    
    # Financial health score calculation
    health_score = 100
    
    # Reduce score based on various factors
    if metrics.get('recent_3_months'):
        recent_net_flow = metrics['recent_3_months']['net_flow']
        if recent_net_flow < 0:
            health_score -= 20  # Negative cash flow
        
        avg_monthly_expenses = metrics['recent_3_months']['avg_monthly_expenses']
        if avg_monthly_expenses > 0:
            runway_months = current_net_worth / avg_monthly_expenses
            if runway_months < 3:
                health_score -= 30
            elif runway_months < 6:
                health_score -= 15
    
    # Income stability impact
    if metrics.get('income_analysis'):
        stability = metrics['income_analysis']['income_stability_score']
        if stability < 70:
            health_score -= 15
        elif stability < 50:
            health_score -= 25
    
    metrics['financial_health_score'] = max(0, health_score)
    
    # Savings rate calculation
    if balance_history and len(balance_history) >= 2:
        time_period_months = metrics.get('tracking_period_months', 1)
        total_net_worth_change = metrics.get('total_net_worth_change', 0)
        
        if time_period_months > 0 and total_income > 0:
            savings_rate = (total_net_worth_change / total_income) * 100
            metrics['savings_rate'] = savings_rate
    
    return metrics

def create_comprehensive_financial_context(financial_data):
    """
    Creates an extremely detailed financial context with all calculations and metrics.
    """
    if not financial_data:
        return "No financial data available to analyze."
    
    # Get comprehensive metrics
    metrics = calculate_comprehensive_metrics(financial_data)
    
    # Build detailed context
    context_parts = []
    
    # Header
    context_parts.append("=== COMPREHENSIVE FINANCIAL ANALYSIS FOR MOUMEN ===\n\n")
    
    # Current Financial Position
    context_parts.append("### CURRENT FINANCIAL POSITION\n")
    current_net_worth = metrics.get('current_net_worth', 0)
    net_worth_date = metrics.get('net_worth_as_of_date', 'Unknown')
    context_parts.append(f"• Current Net Worth (Bank Balance): {current_net_worth:,.2f} MAD\n")
    context_parts.append(f"• As of Date: {net_worth_date}\n")
    
    if metrics.get('total_net_worth_change') is not None:
        change = metrics['total_net_worth_change']
        change_pct = metrics.get('net_worth_change_percentage', 0)
        period_months = metrics.get('tracking_period_months', 0)
        context_parts.append(f"• Net Worth Change: {change:,.2f} MAD ({change_pct:+.1f}%)\n")
        context_parts.append(f"• Tracking Period: {period_months:.1f} months\n")
    
    context_parts.append("\n")
    
    # Cash Flow Analysis
    context_parts.append("### CASH FLOW ANALYSIS\n")
    total_income = metrics.get('total_income_all_time', 0)
    total_expenses = metrics.get('total_expenses_all_time', 0)
    net_flow = metrics.get('net_cash_flow_all_time', 0)
    
    context_parts.append(f"• Total Income (All Time): {total_income:,.2f} MAD\n")
    context_parts.append(f"• Total Expenses (All Time): {total_expenses:,.2f} MAD\n")
    context_parts.append(f"• Net Cash Flow (All Time): {net_flow:,.2f} MAD\n")
    
    # Recent performance
    if metrics.get('recent_3_months'):
        recent = metrics['recent_3_months']
        context_parts.append(f"• Recent 3 Months Income: {recent['income']:,.2f} MAD\n")
        context_parts.append(f"• Recent 3 Months Expenses: {recent['expenses']:,.2f} MAD\n")
        context_parts.append(f"• Recent 3 Months Net Flow: {recent['net_flow']:,.2f} MAD\n")
        context_parts.append(f"• Average Monthly Income: {recent['avg_monthly_income']:,.2f} MAD\n")
        context_parts.append(f"• Average Monthly Expenses: {recent['avg_monthly_expenses']:,.2f} MAD\n")
        
        # Calculate runway
        if recent['avg_monthly_expenses'] > 0:
            runway = current_net_worth / recent['avg_monthly_expenses']
            context_parts.append(f"• Financial Runway: {runway:.1f} months\n")
    
    context_parts.append("\n")
    
    # Monthly Breakdown
    if metrics.get('monthly_summary'):
        context_parts.append("### MONTHLY BREAKDOWN\n")
        for month_data in metrics['monthly_summary'][-6:]:  # Last 6 months
            context_parts.append(
                f"• {month_data['month']}: Income {month_data['income']:,.2f} MAD, "
                f"Expenses {month_data['expenses']:,.2f} MAD, "
                f"Net {month_data['net_flow']:,.2f} MAD "
                f"({month_data['transaction_count']} transactions)\n"
            )
        context_parts.append("\n")
    
    # Income Analysis
    if metrics.get('income_analysis'):
        income_analysis = metrics['income_analysis']
        context_parts.append("### INCOME ANALYSIS\n")
        context_parts.append(f"• Average Monthly Income: {income_analysis['average_monthly_income']:,.2f} MAD\n")
        context_parts.append(f"• Income Stability Score: {income_analysis['income_stability_score']:.1f}/100\n")
        context_parts.append(f"• Income Volatility: {income_analysis['income_volatility']:.1f}%\n\n")
    
    # Expense Categories
    if metrics.get('expense_categories'):
        context_parts.append("### EXPENSE BREAKDOWN BY CATEGORY\n")
        categories = metrics['expense_categories']
        total_categorized = sum(cat['total'] for cat in categories.values())
        
        for category, data in sorted(categories.items(), key=lambda x: x[1]['total'], reverse=True):
            percentage = (data['total'] / total_categorized * 100) if total_categorized > 0 else 0
            context_parts.append(
                f"• {category.replace('_', ' ').title()}: {data['total']:,.2f} MAD "
                f"({percentage:.1f}%) - {data['count']} transactions\n"
            )
        context_parts.append("\n")
    
    # Recurring Expenses
    if metrics.get('recurring_expenses'):
        context_parts.append("### RECURRING/SUBSCRIPTION EXPENSES\n")
        recurring = metrics['recurring_expenses']
        for pattern, data in sorted(recurring.items(), key=lambda x: x[1]['total_amount'], reverse=True)[:10]:
            context_parts.append(
                f"• '{pattern}': {data['count']} occurrences, "
                f"Total: {data['total_amount']:,.2f} MAD, "
                f"Average: {data['avg_amount']:,.2f} MAD\n"
            )
        context_parts.append("\n")
    
    # Largest Expenses
    if metrics.get('largest_expenses'):
        context_parts.append("### TOP 10 LARGEST EXPENSES\n")
        for i, expense in enumerate(metrics['largest_expenses'][:10], 1):
            context_parts.append(
                f"{i}. {expense['transaction_date']}: {expense['description']} - {expense['debit']:,.2f} MAD\n"
            )
        context_parts.append("\n")
    
    # Financial Health
    health_score = metrics.get('financial_health_score', 0)
    context_parts.append("### FINANCIAL HEALTH ASSESSMENT\n")
    context_parts.append(f"• Overall Financial Health Score: {health_score:.0f}/100\n")
    
    if metrics.get('savings_rate') is not None:
        savings_rate = metrics['savings_rate']
        context_parts.append(f"• Savings Rate: {savings_rate:.1f}%\n")
    
    # Health interpretation
    if health_score >= 80:
        health_status = "EXCELLENT - Strong financial position"
    elif health_score >= 60:
        health_status = "GOOD - Solid financial health with room for improvement"
    elif health_score >= 40:
        health_status = "FAIR - Some financial concerns to address"
    else:
        health_status = "POOR - Significant financial challenges"
    
    context_parts.append(f"• Health Status: {health_status}\n\n")
    
    # Balance History for Trend Analysis
    if metrics.get('balance_history'):
        context_parts.append("### BALANCE HISTORY TREND\n")
        balance_history = metrics['balance_history']
        # Sort by date safely
        balance_history.sort(key=lambda x: x.get('date') or '1900-01-01')
        for balance_point in balance_history[-12:]:  # Last 12 data points
            context_parts.append(f"• {balance_point['date']}: {balance_point['balance']:,.2f} MAD\n")
        context_parts.append("\n")
    
    # Raw Data Summary
    context_parts.append("### DATA SOURCES SUMMARY\n")
    monthly_statements = [d for d in financial_data if d.get('document_type') == 'monthly_statement']
    transaction_lists = [d for d in financial_data if d.get('document_type') == 'transaction_list']
    
    context_parts.append(f"• Monthly Statements: {len(monthly_statements)} documents\n")
    context_parts.append(f"• Transaction Lists: {len(transaction_lists)} documents\n")
    
    total_transactions = sum(len(doc.get('transactions', [])) for doc in financial_data)
    context_parts.append(f"• Total Transactions Analyzed: {total_transactions}\n")
    
    if monthly_statements:
        earliest_start = min((s.get('statement_period', {}).get('start_date') or '9999-12-31') for s in monthly_statements)
        latest_end = max((s.get('statement_period', {}).get('end_date') or '1900-01-01') for s in monthly_statements)
        if earliest_start != '9999-12-31' and latest_end != '1900-01-01':
            context_parts.append(f"• Data Period: {earliest_start} to {latest_end}\n")
    
    context_parts.append("\n")
    context_parts.append("=== END OF FINANCIAL ANALYSIS ===\n")
    
    return "".join(context_parts)

# --- PDF Analysis Logic (keeping existing functions) ---
# Note: These functions have been updated to not call genai.configure() directly.
# The API key will be configured in the main calling function.
def identify_pdf_type(text_content):
    """
    Analyzes the PDF text content to determine the type of bank document.
    Returns: 'monthly_statement', 'transaction_list', or 'unknown'
    """
    text_upper = text_content.upper()
    
    # Keywords that indicate a monthly statement
    monthly_indicators = [
        'RELEVE DE COMPTE',
        'COMPTE BANCAIRE',
        'SOLDE DEPART',
        'SOLDE FINAL',
        'TOTAL MOUVEMENTS'
    ]
    
    # Keywords that indicate a transaction list
    transaction_indicators = [
        'MOUVEMENT DU COMPTE',
        'EDITÉ LE',
        'SOLDE RÉEL',
        'OPÉRATIONS EN COURS'
    ]
    
    monthly_score = sum(1 for indicator in monthly_indicators if indicator in text_upper)
    transaction_score = sum(1 for indicator in transaction_indicators if indicator in text_upper)
    
    if monthly_score >= 2:
        return 'monthly_statement'
    elif transaction_score >= 2:
        return 'transaction_list'
    else:
        return 'unknown'

def create_monthly_statement_prompt():
    """Creates the prompt for monthly bank statements."""
    return """
    Analyze the provided content, which is a monthly bank statement from Attijariwafa bank.
    Extract all information and structure it as a single JSON object.
    The currency is DIRHAM (MAD). All monetary values should be floats.
    Dates must be in YYYY-MM-DD format.

    The desired JSON schema is as follows:
    {
      "document_type": "monthly_statement",
      "bank_name": "string", 
      "agency": "string",
      "account_holder": { "name": "string", "address": "string" },
      "account_details": { "account_number": "string", "full_bank_id": "string", "currency": "string" },
      "statement_period": { "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" },
      "summary": { "opening_balance": "float", "closing_balance": "float", "total_debits": "float", "total_credits": "float" },
      "transactions": [
        { "transaction_date": "YYYY-MM-DD", "value_date": "YYYY-MM-DD", "description": "string", "debit": "float or null", "credit": "float or null" }
      ]
    }

    If a piece of information is not found, use `null`.
    Your response MUST be ONLY the JSON object, without any surrounding text, explanations, or markdown formatting like ```json.
    """

def create_transaction_list_prompt():
    """Creates the prompt for transaction list documents."""
    return """
    Analyze the provided content, which is a transaction list from Attijariwafa bank.
    This document shows recent transactions but may not have complete statement period info.
    Extract all information and structure it as a single JSON object.
    The currency is DIRHAM (MAD). All monetary values should be floats.
    Dates must be in YYYY-MM-DD format.

    The desired JSON schema is as follows:
    {
      "document_type": "transaction_list",
      "bank_name": "Attijariwafa bank",
      "agency": "string or null",
      "account_holder": { "name": "string or null", "address": "string or null" },
      "account_details": { "account_number": "string or null", "full_bank_id": "string or null", "currency": "MAD" },
      "statement_period": { "start_date": "YYYY-MM-DD or null", "end_date": "YYYY-MM-DD or null" },
      "summary": { "opening_balance": "null", "closing_balance": "float or null", "total_debits": "float", "total_credits": "float" },
      "transactions": [
        { "transaction_date": "YYYY-MM-DD", "value_date": "YYYY-MM-DD or null", "description": "string", "debit": "float or null", "credit": "float or null" }
      ]
    }

    For transaction lists:
    - Calculate total_debits and total_credits from the transaction amounts
    - If there's a "Solde réel" mentioned, use it as closing_balance
    - Extract the period from "Mouvement du compte du X au Y" if available
    - Opening balance should be null for transaction lists
    
    If a piece of information is not found, use `null`.
    Your response MUST be ONLY the JSON object, without any surrounding text, explanations, or markdown formatting like ```json.
    """

def create_unknown_document_prompt():
    """Creates a generic prompt for unknown document types."""
    return """
    Analyze the provided content from Attijariwafa bank.
    Try to extract as much financial information as possible and structure it as a JSON object.
    The currency is DIRHAM (MAD). All monetary values should be floats.
    Dates must be in YYYY-MM-DD format.

    The desired JSON schema is as follows:
    {
      "document_type": "unknown",
      "bank_name": "string or null",
      "agency": "string or null", 
      "account_holder": { "name": "string or null", "address": "string or null" },
      "account_details": { "account_number": "string or null", "full_bank_id": "string or null", "currency": "string or null" },
      "statement_period": { "start_date": "YYYY-MM-DD or null", "end_date": "YYYY-MM-DD or null" },
      "summary": { "opening_balance": "float or null", "closing_balance": "float or null", "total_debits": "float or null", "total_credits": "float or null" },
      "transactions": [
        { "transaction_date": "YYYY-MM-DD or null", "value_date": "YYYY-MM-DD or null", "description": "string", "debit": "float or null", "credit": "float or null" }
      ]
    }

    Extract any transactions you can find, even if the format is different.
    If a piece of information is not found, use `null`.
    Your response MUST be ONLY the JSON object, without any surrounding text, explanations, or markdown formatting like ```json.
    """

def get_appropriate_prompt(pdf_type):
    """Returns the appropriate prompt based on PDF type."""
    if pdf_type == 'monthly_statement':
        return create_monthly_statement_prompt()
    elif pdf_type == 'transaction_list':
        return create_transaction_list_prompt()
    else:
        return create_unknown_document_prompt()

def _analyze_pdf_direct(pdf_data, pdf_type):
    """Enhanced direct PDF analysis with type-specific prompts."""
    try:
        client = genai.GenerativeModel('gemini-1.5-flash-latest')
        prompt = get_appropriate_prompt(pdf_type)
        
        response = client.generate_content(
            [{"mime_type": "application/pdf", "data": pdf_data}, prompt],
            generation_config={"temperature": 0.1}
        )
        
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned_text)

    except Exception as e:
        print(f"--- ❌ Direct PDF analysis failed: {e} ---")
        return None

def _analyze_pdf_with_text_extraction(pdf_data, pdf_type):
    """Enhanced fallback text extraction with type-specific prompts."""
    try:
        print("--- Attempting fallback processing using text extraction... ---")
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_data))
        extracted_text = "".join(page.extract_text() for page in pdf_reader.pages)
        
        if not extracted_text or len(extracted_text.strip()) < 50:
            print("--- ❌ Fallback failed: Could not extract sufficient text from PDF. ---")
            return None

        # Determine PDF type from extracted text if not already determined
        if pdf_type == 'unknown':
            pdf_type = identify_pdf_type(extracted_text)
            print(f"--- Identified PDF type from text: {pdf_type} ---")

        client = genai.GenerativeModel('gemini-1.5-flash-latest')
        prompt = get_appropriate_prompt(pdf_type)
        
        text_prompt = f"""
        {prompt}

        Here is the extracted text content from the PDF:
        ---
        {extracted_text[:30000]} 
        ---
        """

        response = client.generate_content(
            text_prompt,
            generation_config={"temperature": 0.1}
        )
        
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned_text)

    except Exception as e:
        print(f"--- ❌ Fallback text analysis failed: {e} ---")
        return None

def post_process_extracted_data(extracted_data):
    """
    Post-processes the extracted data to ensure consistency and fill gaps.
    """
    if not extracted_data:
        return None
    
    # Ensure document_type is set
    if 'document_type' not in extracted_data:
        extracted_data['document_type'] = 'unknown'
    
    # Set default bank name if missing
    if not extracted_data.get('bank_name'):
        extracted_data['bank_name'] = 'Attijariwafa bank'
    
    # Ensure currency is set
    if extracted_data.get('account_details') and not extracted_data['account_details'].get('currency'):
        extracted_data['account_details']['currency'] = 'MAD'
    
    # Calculate missing totals for transaction lists
    if extracted_data.get('document_type') == 'transaction_list':
        transactions = extracted_data.get('transactions', [])
        if transactions:
            total_debits = sum(t.get('debit', 0) or 0 for t in transactions)
            total_credits = sum(t.get('credit', 0) or 0 for t in transactions)
            
            if not extracted_data.get('summary'):
                extracted_data['summary'] = {}
            
            if extracted_data['summary'].get('total_debits') is None:
                extracted_data['summary']['total_debits'] = total_debits
            if extracted_data['summary'].get('total_credits') is None:
                extracted_data['summary']['total_credits'] = total_credits
    
    # Validate and fix transaction dates
    if extracted_data.get('transactions'):
        for transaction in extracted_data['transactions']:
            # Ensure transaction_date is present
            if not transaction.get('transaction_date') and transaction.get('value_date'):
                transaction['transaction_date'] = transaction['value_date']
            
            # Ensure at least one amount is present
            if not transaction.get('debit') and not transaction.get('credit'):
                # Try to parse from description if possible
                continue
    
    return extracted_data

def smart_merge_data(existing_data, new_data):
    """
    Intelligently merges new data with existing data.
    Handles both monthly statements and transaction lists.
    """
    if not existing_data:
        return [new_data]
    
    new_hash = new_data.get('source_file_hash')
    
    # Check if this exact file was already processed
    for i, stmt in enumerate(existing_data):
        if stmt.get('source_file_hash') == new_hash:
            print(f"--- File already exists, updating data ---")
            existing_data[i] = new_data
            return existing_data
    
    # For transaction lists, try to merge with existing monthly statements
    if new_data.get('document_type') == 'transaction_list':
        merged = False
        new_transactions = new_data.get('transactions', [])
        
        for stmt in existing_data:
            if stmt.get('document_type') == 'monthly_statement':
                # Check if transactions overlap with existing statement period
                stmt_start = stmt.get('statement_period', {}).get('start_date')
                stmt_end = stmt.get('statement_period', {}).get('end_date')
                
                if stmt_start and stmt_end:
                    # Check if any new transactions fall within this period
                    overlapping_transactions = []
                    for trans in new_transactions:
                        trans_date = trans.get('transaction_date')
                        if trans_date and stmt_start <= trans_date <= stmt_end:
                            overlapping_transactions.append(trans)
                    
                    if overlapping_transactions:
                        print(f"--- Found overlapping transactions, merging with existing statement ---")
                        # Add new transactions that don't already exist
                        existing_transactions = stmt.get('transactions', [])
                        existing_descriptions = [t.get('description', '') + str(t.get('transaction_date', '')) for t in existing_transactions]
                        
                        for new_trans in overlapping_transactions:
                            new_desc_date = new_trans.get('description', '') + str(new_trans.get('transaction_date', ''))
                            if new_desc_date not in existing_descriptions:
                                existing_transactions.append(new_trans)
                        
                        # Update totals
                        total_debits = sum(t.get('debit', 0) or 0 for t in existing_transactions)
                        total_credits = sum(t.get('credit', 0) or 0 for t in existing_transactions)
                        stmt['summary']['total_debits'] = total_debits
                        stmt['summary']['total_credits'] = total_credits
                        
                        merged = True
                        break
        
        if not merged:
            # Add as separate entry if no overlap found
            existing_data.append(new_data)
    else:
        # For monthly statements, just add to the list
        existing_data.append(new_data)
    
    # Sort by end date
    existing_data.sort(key=lambda x: x.get('statement_period', {}).get('end_date', '') or '1900-01-01')
    return existing_data

def analyze_pdf_with_smart_detection(pdf_data, filename, api_key):
    """
    Enhanced PDF analysis that takes an API key as an argument.
    """
    # ** NEW: Configure GenAI with the user-provided key **
    try:
        genai.configure(api_key=api_key)
    except Exception as e:
        print(f"--- ❌ Failed to configure Gemini with provided API key: {e}")
        # This will raise an AuthenticationError if the key is invalid
        raise ValueError(f"Invalid or improperly formatted Gemini API Key. {e}")


    print(f"\nProcessing '{filename}'...")
    
    # First, try to determine PDF type from filename
    pdf_type = 'unknown'
    filename_lower = filename.lower()
    if 'statement' in filename_lower or 'releve' in filename_lower:
        pdf_type = 'monthly_statement'
    elif 'operation' in filename_lower or 'mouvement' in filename_lower or 'transaction' in filename_lower:
        pdf_type = 'transaction_list'
    
    print(f"--- Initial type guess from filename: {pdf_type} ---")
    print("--- Attempting Method 1: Direct PDF Analysis ---")
    
    extracted_data = _analyze_pdf_direct(pdf_data, pdf_type)

    if extracted_data:
        print("--- ✅ Success with Direct PDF Analysis ---")
        extracted_data['processed_with_fallback'] = False
    else:
        extracted_data = _analyze_pdf_with_text_extraction(pdf_data, pdf_type)
        if extracted_data:
            print("--- ✅ Success with Text Extraction Fallback ---")
            extracted_data['processed_with_fallback'] = True

    if not extracted_data:
        print("--- ❌ Both analysis methods failed. ---")
        return None
    
    # Post-process the data
    extracted_data = post_process_extracted_data(extracted_data)
    
    # Add metadata
    file_hash = hashlib.sha256(pdf_data).hexdigest()
    extracted_data['source_file_hash'] = file_hash
    extracted_data['source_file_name'] = filename
    extracted_data['processing_timestamp'] = datetime.now().isoformat()
    
    print(f"--- ✅ Successfully processed as {extracted_data.get('document_type', 'unknown')} ---")
    return extracted_data

# --- API Endpoints ---
@app.route('/api/get-financial-data', methods=['GET'])
def get_financial_data():
    """Endpoint to fetch all stored financial data."""
    if not os.path.exists(OUTPUT_JSON_PATH):
        return jsonify([])
    try:
        with open(OUTPUT_JSON_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Failed to read data file: {e}"}), 500

@app.route('/api/get-financial-metrics', methods=['GET'])
def get_financial_metrics():
    """New endpoint to get comprehensive financial metrics and calculations."""
    if not os.path.exists(OUTPUT_JSON_PATH):
        return jsonify({"error": "No financial data available"}), 404
    
    try:
        with open(OUTPUT_JSON_PATH, 'r', encoding='utf-8') as f:
            financial_data = json.load(f)
        
        if not financial_data:
            return jsonify({"error": "No financial data available"}), 404
        
        # Calculate comprehensive metrics
        metrics = calculate_comprehensive_metrics(financial_data)
        return jsonify(metrics)
        
    except Exception as e:
        return jsonify({"error": f"Failed to calculate metrics: {e}"}), 500

@app.route('/api/upload-statement', methods=['POST'])
def upload_statement():
    """Enhanced endpoint to upload and analyze any type of bank PDF."""
    # ** NEW: Get API key from request header **
    user_api_key = request.headers.get('X-Gemini-API-Key')
    if not user_api_key:
        return jsonify({"error": "Gemini API key is missing. Please provide it in the X-Gemini-API-Key header."}), 400
        
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file and file.filename.endswith('.pdf'):
        pdf_data = file.read()
        filename = file.filename
        
        try:
            # ** NEW: Pass the user's API key to the analysis function **
            new_statement_data = analyze_pdf_with_smart_detection(pdf_data, filename, user_api_key)
        except ValueError as e:
             # This catches invalid API key errors from our analysis function
            return jsonify({"error": str(e)}), 401 # 401 Unauthorized is appropriate for bad keys
        except Exception as e:
            # Catch other unexpected errors
            print(f"An unexpected error occurred during PDF analysis: {e}")
            return jsonify({"error": f"An unexpected server error occurred: {e}"}), 500

        if not new_statement_data:
            return jsonify({ 
                "error": "Failed to extract data from PDF. The PDF may be an image, password-protected, or not a supported bank document format." 
            }), 500

        # Load existing data
        all_statements_data = []
        if os.path.exists(OUTPUT_JSON_PATH):
            with open(OUTPUT_JSON_PATH, 'r', encoding='utf-8') as f:
                try: 
                    all_statements_data = json.load(f)
                except json.JSONDecodeError: 
                    pass # Overwrite if corrupt

        # Smart merge with existing data
        all_statements_data = smart_merge_data(all_statements_data, new_statement_data)
        
        # Save updated data
        with open(OUTPUT_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(all_statements_data, f, indent=2, ensure_ascii=False)
            
        return jsonify({
            "message": f"File processed successfully as {new_statement_data.get('document_type', 'unknown')}", 
            "data": new_statement_data
        })

    return jsonify({"error": "Invalid file type, only PDF is allowed."}), 400

@app.route('/api/chat', methods=['POST'])
def chat():
    """Enhanced chat endpoint with comprehensive financial analysis capabilities."""
    # ** NEW: Get API key from request header **
    user_api_key = request.headers.get('X-Gemini-API-Key')
    if not user_api_key:
        return jsonify({"error": "Gemini API key is missing. Please provide it in the X-Gemini-API-Key header."}), 400

    data = request.get_json()
    user_message = data.get('message')
    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    # Load financial data and create comprehensive context
    financial_context = "No financial data has been uploaded yet."
    raw_financial_data = []
    
    if os.path.exists(OUTPUT_JSON_PATH):
        try:
            with open(OUTPUT_JSON_PATH, 'r', encoding='utf-8') as f:
                raw_financial_data = json.load(f)
                if raw_financial_data:
                    financial_context = create_comprehensive_financial_context(raw_financial_data)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"Could not read or parse financial data file: {e}")
            financial_context = "Error: Could not read financial data file."

    # Enhanced prompt with calculation capabilities
    prompt = f"""You are SmartFin AI, Moumen's expert financial advisor and analyst. When the user talk normal just chat normal and when give you a tassk do it and ansswer bassed on what he want

You have access to COMPLETE financial data and comprehensive analysis. You can answer ANY financial question with precise calculations and insights.

KEY CAPABILITIES:
- Calculate net worth (current bank balance)
- Analyze spending patterns and trends
- Calculate income stability and growth
- Determine savings rates and financial health
- Provide detailed expense breakdowns
- Identify recurring payments and subscriptions
- Calculate financial runway and liquidity
- Analyze cash flow patterns
- Compare monthly/quarterly performance
- Make financial projections and recommendations

FINANCIAL ANALYSIS METHODOLOGY:
- Net Worth = Current bank balance (most recent closing balance)
- Savings Rate = (Net Worth Change / Total Income) × 100
- Financial Runway = Current Balance / Average Monthly Expenses
- Income Stability = Based on monthly income variance analysis
- Expense Analysis = Categorized by transaction patterns
- Health Score = Composite score based on multiple financial factors

When user asks about:
- "Net worth" → Provide current bank balance and trend analysis
- "Spending" → Break down by categories with specific amounts
- "Income" → Show stability, trends, and monthly patterns
- "Savings" → Calculate rates and provide recommendations
- "Budget" → Analyze expenses vs income with suggestions
- Any financial metric → Calculate it from the available data

RESPONSE STYLE:
- Be direct and specific with numbers
- Show calculations when relevant
- Provide actionable insights
- Use MAD currency format
- Reference specific dates and transactions when helpful
- Always base answers on the actual data provided

--- COMPREHENSIVE FINANCIAL ANALYSIS ---
{financial_context}
--- END OF FINANCIAL DATA ---

User's question: "{user_message}"

Provide a detailed, data-driven answer with specific numbers and insights from the analysis above.
"""

    try:
        # ** NEW: Configure GenAI with the user-provided key for this request **
        genai.configure(api_key=user_api_key)
        client = genai.GenerativeModel('gemini-1.5-flash-latest') # Changed to 1.5-flash for consistency
        response = client.generate_content(
            prompt,
            generation_config={"temperature": 0.2}
        )
        return jsonify({"reply": response.text})
    except Exception as e:
        print(f"Error in chat endpoint: {e}")
        # Provide a more specific error for invalid keys
        if "API_KEY_INVALID" in str(e):
            return jsonify({"error": "The provided Gemini API key is invalid. Please check it in the settings."}), 401
        return jsonify({"error": "Sorry, I couldn't process that request due to a server-side AI error."}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5001)