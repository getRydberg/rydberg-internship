"""Reusable application answers with field-level provenance."""
import json
import re

from .resume import contact_hints

FIELDS = {
    'full_name':'Full name', 'email':'Email', 'phone':'Phone', 'location':'Current location',
    'school':'School', 'degree':'Degree / major', 'graduation':'Expected graduation',
    'linkedin':'LinkedIn URL', 'portfolio':'Portfolio / GitHub URL',
    'availability':'Availability', 'work_authorization':'Work authorization',
    'sponsorship':'Will you need sponsorship?', 'experience':'Relevant experience',
    'motivation':'Why this role / company?', 'cover_letter':'Cover letter',
}
LONG_FIELDS = {'experience','motivation','cover_letter'}


def question_key(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


def suggestions(db, user, role_id, question=''):
    result = {}
    rows = db.execute('''SELECT a.answers,r.company,r.role FROM application_history a
        JOIN roles r ON r.id=a.role_id WHERE a.user_id=? AND a.role_id!=? ORDER BY a.id DESC LIMIT 200''',
        (user,role_id)).fetchall()
    for row in rows:
        answers = json.loads(row['answers'])
        for key in FIELDS:
            if answers.get(key, '').strip() and key not in result:
                result[key] = {'value':answers[key], 'source':f"{row['company']} — {row['role']}"}
        if question and question_key(question) == question_key(answers.get('question','')) and answers.get('answer','').strip():
            result.setdefault('answer', {'value':answers['answer'], 'source':f"{row['company']} — {row['role']}"})
    row = db.execute('SELECT display_name,details,resume_text FROM users WHERE id=?', (user,)).fetchone()
    details = json.loads(row['details'])
    for key, value in details.items():
        if key in FIELDS and value.strip():
            result.setdefault(key, {'value':value, 'source':'Your saved applicant profile'})
    for key, value in contact_hints(row['resume_text']).items():
        result.setdefault(key, {'value':value, 'source':'Your uploaded resume'})
    if row['display_name']:
        result.setdefault('full_name', {'value':row['display_name'], 'source':'Your account name'})
    return result


def parse_answers(form):
    answers = {key:form.get(key,'').strip() for key in [*FIELDS,'question','answer']}
    for key, value in answers.items():
        limit = 10000 if key in LONG_FIELDS or key == 'answer' else 1000
        if len(value) > limit or '\x00' in value:
            raise ValueError(f'{FIELDS.get(key,key)} exceeds {limit} characters or contains invalid text.')
    return answers
