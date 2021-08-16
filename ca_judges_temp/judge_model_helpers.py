import re
from cl.corpus_importer.court_regexes import match_court_string 
from cl.people_db.lookup_utils import lookup_judge_by_full_name

def find_or_create_judge(judgeJson): 
  # get the judge's positions
  positions = judgeJson['positions']

  courts = []
  last_first_middle = None
  let judge = None

  # Since we search for judge by court_id and judge name
  # first grab all the court names as well as the last_first_middle
  for position in positions:
    courts.append(position['orgName'])
    if last_first_middle == None:
      last_first_middle = position['lastFirstMiddleName'] 
  
  for court in courts:
    # try to find the court frtom CL
    court_id = find_court(court)
    if not court_id:
      return None

    search = lookup_judge_by_full_name(
      name=last_first_middle
      court_id=court_id
    )
    if (search is not None):
      judge = search
      break

  return judge


def parse_name(last_first_middle, first, last): 
  # examples
  # Aranda, Benjamin J. III
  # Cherniss, Sidney A., Jr.
  # Nelson, Mark G., Sr.

# Lookup court by name
def find_court(org_type, org_name):
  # Association
  # County Counsel
  # Court Not CA Judiciary [i.e., Federal]
  # Court of Appeal
  # JCC Agency
  # Justice Court
  # Law Firm
  # Municipal Court
  # Non-Judicial Other
  # Superior Court
  # Supreme Court

  # search the org_type
  court_of_appeal = re.compile(r'Appeal$')
  justice_court = re.compile(r'Justice\sCourt')
  municipal_court = re.compile(r'Municipal')
  superior_court = re.compile(r'Superior\sCourt')
  supreme_court = re.compile(r'Supreme\sCourt')

  # search the org_name for U.S. District
  federal_court = re.compile(r'U\.[\s]?S\.\sDistrict')
 
  if court_of_appeal.search(org_type):
    return 'calctapp'
  elfif justice_court.search(org_type):
    # TODO: CONFIRM WITH MIKE SINCE NOT EXIST
    return 'caljusticect'
  elfif municipal_court.search(org_type):
    # TODO: CONFIRM WITH MIKE SINCE NOT EXIST
    return 'calmunict'
  elfif superior_court.search(org_type):
    # TODO: CONFIRM WITH MIKE SINCE NOT EXIST
    return 'calsupct'
  elfif supreme_court.search(org_type):
    return 'cal'
  # if no court matches, but org_name contains district regex
  # it's a District Federal Court (exact district not provided)
  elfif federal_court.search(org_name):
    return 'district_federal'
  else:
    return None
