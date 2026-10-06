"""Conservative, source-backed location and internship term normalization."""
import re

STATES = dict(zip(
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split(),
    ['Alabama','Alaska','Arizona','Arkansas','California','Colorado','Connecticut','Delaware','Florida','Georgia','Hawaii','Idaho','Illinois','Indiana','Iowa','Kansas','Kentucky','Louisiana','Maine','Maryland','Massachusetts','Michigan','Minnesota','Mississippi','Missouri','Montana','Nebraska','Nevada','New Hampshire','New Jersey','New Mexico','New York','North Carolina','North Dakota','Ohio','Oklahoma','Oregon','Pennsylvania','Rhode Island','South Carolina','South Dakota','Tennessee','Texas','Utah','Vermont','Virginia','Washington','West Virginia','Wisconsin','Wyoming','District of Columbia']))
CITY_STATES = {'nyc':'NY','new york city':'NY','santa clara':'CA','san francisco':'CA',
               'san jose':'CA','seattle':'WA','boston':'MA','austin':'TX','secaucus':'NJ',
               'chicago':'IL','los angeles':'CA','palo alto':'CA','mountain view':'CA',
               'san diego':'CA','washington dc':'DC','washington, dc':'DC'}
SEASONS = {'winter':1, 'spring':2, 'summer':3, 'fall':4}
COUNTRIES = {'US':'United States', 'CA':'Canada', 'GB':'United Kingdom',
             'IN':'India', 'DE':'Germany', 'UNKNOWN':'Unspecified'}


def location_metadata(location):
    text = location.casefold().strip()
    state = ''
    country = 'UNKNOWN'
    if re.search(r'\b(canada|toronto|vancouver|montreal|montréal|waterloo|ottawa|calgary|edmonton|quebec|ontario|british columbia)\b|,\s*(ON|BC|AB|QC|NS|NB|NL|PE|SK|MB|YT|NT|NU)\b', location, re.I):
        country = 'CA'
    elif re.search(r'\b(uk|united kingdom|london|england|scotland|belfast)\b', text):
        country = 'GB'
    elif re.search(r'\b(india|bengaluru|bangalore|hyderabad|mumbai)\b', text):
        country = 'IN'
    elif re.search(r'\b(germany|berlin|munich)\b', text):
        country = 'DE'
    else:
        # Abbreviations must follow a comma or a US/remote qualifier: "IN" alone is ambiguous.
        found = re.search(r',\s*(' + '|'.join(STATES) + r')\b', location)
        if found:
            state = found[1]
        if not state:
            for code, name in STATES.items():
                if re.search(r'\b' + re.escape(name.casefold()) + r'\b', text):
                    state = code
                    break
        state = CITY_STATES.get(text, '') or state
        if state or re.search(r'\b(us|usa|united states|u\.s\.)\b', text):
            country = 'US'
    return {'country':country, 'state':state, 'remote':bool(re.search(r'\bremote\b', text))}


def parse_terms(text, default=None):
    """Resolve shared years in 'Summer & Fall 2027', preserving distinct year groups."""
    terms, pending = [], []
    tokens = re.findall(r'\b(winter|spring|summer|fall|autumn|20\d{2})\b', text, re.I)
    for token in tokens:
        token = token.lower()
        if token.isdigit():
            for season in pending:
                terms.append(f'{season.title()} {token}')
            pending = []
        else:
            pending.append('fall' if token == 'autumn' else token)
    if pending and terms:
        year = terms[-1].split()[1]
        terms.extend(f'{season.title()} {year}' for season in pending)
    if not terms and default:
        defaults = parse_terms(default)
        if pending and defaults:
            terms = [f'{season.title()} {defaults[0].split()[1]}' for season in pending]
        else:
            terms = defaults
    return sorted(set(terms), key=lambda term: (int(term.split()[1]), SEASONS[term.split()[0].lower()]))


def role_metadata(role, source=''):
    default = None
    if 'Off-Season' not in source:
        found = re.search(r'Summer(20\d{2})-Internships', source)
        if found:
            default = 'Summer ' + found[1]
    terms = role.get('terms') or parse_terms(role['role'], default)
    coop = bool(re.search(r'\bco[\s-]?op\b|\bcooperative\b', role['role'], re.I))
    return terms, coop
