import io
import json
import re
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from internship.config import DEFAULT_SOURCE, OFFSEASON_SOURCE
from internship.metadata import location_metadata, parse_terms
from internship.parser import parse_readme
from internship.storage import connect, ingest, initialize, score_and_queue
from internship.web import create_app
from test_parser import row, table
from test_storage import SEED, StorageCase


def pdf_bytes(text='Sean Kim, sean@example.com. Firmware, Rust, Python, embedded software and Linux.'):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'):NameObject('/Font'), NameObject('/Subtype'):NameObject('/Type1'),
                             NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(('BT /F1 12 Tf 40 700 Td (' + text + ') Tj ET').encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class WorkflowTests(StorageCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({'TESTING':True, 'AUTH_MODE':'name', 'DATABASE':self.path,
                               'PROFILE_SEED':str(SEED), 'SOURCES':[DEFAULT_SOURCE, OFFSEASON_SOURCE]})
        self.client = self.app.test_client()
        self.roles = parse_readme(table(
            row('SpringCo','Firmware Spring 2027 Intern','New York, NY') +
            row('FallCo','Embedded Co-op - Summer & Fall 2027','Santa Clara, CA',app='<a href="https://jobs.example/2">Apply</a>') +
            row('CanadaCo','Software Intern','Toronto, ON',app='<a href="https://jobs.example/3">Apply</a>') +
            row('RemoteCo','Software Intern','Remote in USA',app='<a href="https://jobs.example/4">Apply</a>')))
        with connect(self.path) as db:
            ingest(db, DEFAULT_SOURCE, self.roles)
        self.login(self.client, 'Sean Kim')

    def token(self, client=None, path='/internship/'):
        html = (client or self.client).get(path).text
        return re.search(r'name="csrf" value="([a-f0-9]+)"', html)[1]

    def login(self, client, name):
        csrf = self.token(client, '/internship/login')
        self.assertEqual(client.post('/internship/login', data={'csrf':csrf,'name':name}).status_code,303)

    def post(self, path, data, client=None, **kwargs):
        client = client or self.client
        return client.post(path, data={'csrf':self.token(client),**data}, **kwargs)

    def application_url(self, index=0):
        return '/internship/roles/' + self.roles[index]['id'] + '/application'

    def test_name_login_persistence_csrf_and_isolation(self):
        url = self.application_url()
        self.assertEqual(self.post(url, {'action':'save','status':'applied','notes':'Sean private','email':'sean@example.com'}).status_code,303)
        other = self.app.test_client()
        self.login(other,'Jane')
        html = other.get(url).text
        self.assertNotIn('Sean private',html)
        self.assertNotIn('sean@example.com',html)
        self.assertEqual(other.post(url,data={'csrf':self.token(),'action':'save'}).status_code,403)
        returned = self.app.test_client()
        self.login(returned,'  SEAN   KIM ')
        self.assertIn('Sean private',returned.get(url).text)
        recreated = create_app({'TESTING':True,'AUTH_MODE':'name','DATABASE':self.path,'PROFILE_SEED':str(SEED)})
        retained = recreated.test_client()
        retained.set_cookie('internship_session',self.client.get_cookie('internship_session').value)
        self.assertIn('Sean private',retained.get(url).text)
        self.assertEqual(self.post('/internship/logout',{}).status_code,303)
        self.assertEqual(self.client.get('/internship/').status_code,401)

    def test_resume_updates_fit_and_is_private_and_removable(self):
        with connect(self.path) as db:
            before = db.execute('SELECT score FROM scores WHERE user_id=? AND role_id=?',('name:sean kim',self.roles[0]['id'])).fetchone()[0]
        response = self.post('/internship/resume',{'resume':(io.BytesIO(pdf_bytes()),'resume.pdf')})
        self.assertEqual(response.status_code,303)
        with connect(self.path) as db:
            score = db.execute('SELECT score,matched FROM scores WHERE user_id=? AND role_id=?',('name:sean kim',self.roles[0]['id'])).fetchone()
            self.assertGreater(score['score'],before)
            self.assertIn('Resume: firmware',json.loads(score['matched']))
            self.assertIn('sean@example.com',db.execute('SELECT resume_text FROM users WHERE id=?',('name:sean kim',)).fetchone()[0])
        other = self.app.test_client()
        self.login(other,'Jane')
        self.assertNotIn('sean@example.com',other.get('/internship/resume').text)
        self.assertNotIn('resume.pdf',other.get('/internship/resume').text)
        self.assertEqual(self.post('/internship/resume',{'action':'remove'}).status_code,303)
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT score FROM scores WHERE user_id=? AND role_id=?',('name:sean kim',self.roles[0]['id'])).fetchone()[0],before)

    def test_pdf_validation_and_csrf(self):
        for payload,name in [(b'%PDF-fake','resume.pdf'), (pdf_bytes(),'resume.html'), (b'not pdf','resume.pdf'), (pdf_bytes(''),'empty.pdf'), (b'%PDF-'+b'x'*(5*1024*1024),'large.pdf')]:
            with self.subTest(name=name):
                response = self.post('/internship/resume',{'resume':(io.BytesIO(payload),name)})
                self.assertEqual(response.status_code,400)
        response = self.client.post('/internship/resume',data={'resume':(io.BytesIO(pdf_bytes()),'resume.pdf')})
        self.assertEqual(response.status_code,403)

    def test_filters_combine_geography_terms_and_program(self):
        html = self.client.get('/internship/?country=US&state=CA&season=fall&year=2027&coop=1').text
        self.assertIn('Application workspace',html)
        self.assertIn('Embedded Co-op - Summer',html)
        self.assertNotIn('Firmware Spring 2027 Intern',html)
        self.assertNotIn('Software Intern</td>',html)
        self.assertIn('Firmware Spring 2027 Intern',self.client.get('/internship/?country=US&state=NY&season=spring&year=2027&coop=0').text)
        self.assertIn('No roles match',self.client.get('/internship/?country=CA&state=CA&eligible=0').text)
        self.assertIn('Software Intern',self.client.get('/internship/?remote=1&country=US').text)
        html = self.client.get('/internship/?sort=term').text
        self.assertLess(html.index('Firmware Spring 2027 Intern'),html.index('Embedded Co-op - Summer'))
        for query in ('country=bad','state=ZZ','season=bad','year=x','coop=2','sort=bad','score=nan'):
            self.assertEqual(self.client.get('/internship/?'+query).status_code,400)

    def test_answer_reuse_provenance_no_overwrite_and_versions(self):
        first,second = self.application_url(0),self.application_url(1)
        old = {'action':'save','status':'not_started','email':'old@example.com','experience':'Built a firmware debugger',
               'question':'Describe a challenging project?','answer':'I traced a bootloader issue.','motivation':'Interested in SpringCo'}
        self.assertEqual(self.post(first,old).status_code,303)
        response = self.post(second,{'action':'assist','status':'not_started','email':'new@example.com',
                                   'question':'describe a challenging project!'})
        self.assertEqual(response.status_code,200)
        self.assertIn('value="new@example.com"',response.text)
        self.assertIn('Built a firmware debugger</textarea>',response.text)
        self.assertIn('I traced a bootloader issue.</textarea>',response.text)
        self.assertIn('SpringCo — Firmware Spring 2027 Intern',response.text)
        with connect(self.path) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM applications WHERE user_id=? AND role_id=?',('name:sean kim',self.roles[1]['id'])).fetchone())
            self.assertIsNone(db.execute('SELECT applied_at FROM tracking WHERE user_id=?',('name:sean kim',)).fetchone()[0])
        self.assertEqual(self.post(first,{**old,'status':'applied','answer':'A revised answer'}).status_code,303)
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM application_history').fetchone()[0],2)
            self.assertIsNotNone(db.execute('SELECT applied_at FROM tracking WHERE user_id=?',('name:sean kim',)).fetchone()[0])

    def test_saved_profile_and_resume_contact_suggestions(self):
        self.assertEqual(self.post('/internship/profile',{'action':'applicant','school':'Example University','work_authorization':'Authorized'}).status_code,303)
        self.post('/internship/resume',{'resume':(io.BytesIO(pdf_bytes()),'resume.pdf')})
        html = self.post(self.application_url(),{'action':'assist'}).text
        self.assertIn('value="Example University"',html)
        self.assertIn('value="sean@example.com"',html)
        self.assertIn('Your uploaded resume',html)
        self.assertIn('Your saved applicant profile',html)

    def test_refresh_replaces_changed_terms_without_losing_application(self):
        url = self.application_url(0)
        self.post(url, {'action':'save', 'status':'applied', 'email':'keep@example.com'})
        changed = {**self.roles[0], 'terms':['Fall 2027']}
        with connect(self.path) as db:
            ingest(db, DEFAULT_SOURCE, [changed, *self.roles[1:]])
            score_and_queue(db, set())
        self.assertIn('No roles match',self.client.get('/internship/?company=SpringCo&season=spring&year=2027').text)
        self.assertIn('Firmware Spring 2027 Intern',self.client.get('/internship/?company=SpringCo&season=fall&year=2027').text)
        self.assertIn('keep@example.com',self.client.get(url).text)

    def test_refresh_ingests_summer_and_six_column_offseason(self):
        offseason = table('<tr><td>Offseason</td><td>Firmware Co-op</td><td>Boston, MA</td><td>Spring 2027, Fall 2027</td><td><a href="https://jobs.example/5">Apply</a></td><td>1d</td></tr>')
        with patch('internship.worker.fetch', side_effect=lambda url: offseason if 'Off-Season' in url else table(row())):
            # crawl's default fetcher is bound at definition time.
            from internship.worker import crawl
            with patch('internship.worker.crawl',side_effect=lambda config: crawl(config,lambda url: offseason if 'Off-Season' in url else table(row()))):
                self.assertEqual(self.post('/internship/refresh',{}).status_code,303)
        html = self.client.get('/internship/?country=US&state=MA&season=fall&year=2027&coop=1').text
        self.assertIn('Firmware Co-op',html)
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sources WHERE last_error IS NULL').fetchone()[0],2)


class MetadataTests(unittest.TestCase):
    def test_source_terms_and_location_conservatism(self):
        self.assertEqual(parse_terms('Summer & Fall 2027'),['Summer 2027','Fall 2027'])
        self.assertEqual(parse_terms('Fall 2026 / Spring 2027'),['Fall 2026','Spring 2027'])
        self.assertEqual(parse_terms('Spring Intern','Summer 2027'),['Spring 2027'])
        self.assertEqual(location_metadata('Remote')['country'],'UNKNOWN')
        self.assertEqual(location_metadata('Toronto, ON')['country'],'CA')
        self.assertEqual(location_metadata('Washington, DC')['state'],'DC')
        self.assertEqual(location_metadata('Atlanta, Georgia')['state'],'GA')

    def test_additive_upgrade_preserves_old_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'old.sqlite3')
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE users(id TEXT PRIMARY KEY,profile TEXT NOT NULL,created_at TEXT NOT NULL)')
                db.execute('INSERT INTO users VALUES(?,?,?)',('legacy@example.com',SEED.read_text(),'2026-01-01'))
            initialize(path)
            initialize(path)
            with connect(path) as db:
                user = db.execute('SELECT * FROM users').fetchone()
                self.assertEqual(user['id'],'legacy@example.com')
                self.assertEqual(user['profile'],SEED.read_text())
                self.assertEqual(user['resume_text'],'')
