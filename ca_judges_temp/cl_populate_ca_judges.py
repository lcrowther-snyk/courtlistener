import json
import re
from django.db import transaction
from django.db.models import Count

from cl.lib.command_utils import VerboseCommand, logger
from cl.people_db.models import Attorney
from cl.people_db.models import AttorneyOrganizationAssociation as AttyOrgAss
from cl.people_db.models import Role
from cl.search.models import Docket

"""
  Shape of Loaded File JSON
  {
    count: string,
    judges: Judge[]
  }
"""

"""
  Shape of Judge JSON
  {
    fullName: string
    positions: Position[]
  }
"""

"""
  Shape of Position JSON
  All properties are strings
  {
    id,
    salutation,
    jobTitle,
    title,
    fullName,
    lastFirstMiddleName,
    firstName,
    lastName,
    judicialIndicator,
    orgType,
    orgName,
    orgDivision,
    experienceStatus,
    experienceStatusReason,
    experienceStatusEffectiveDate,
    expId,
    barAdmissionDate,
    deceasedDate,
    judicialPositionId,
    judicialPositionJobClass,
    judicialPositionJobTitle,
    judicialPositionOrgType,
    judicialPositionOrgName,
    judicialPositionLocationName,
    judicialExperienceDivsionName,
    judicialPositionOrgCounty,
    judicialPositionActiveDate,
    judicialPositionInactiveDate,
    judicialExperiencePendingStatus,
    judicialExperiencePendingSubType,
    JudicialExperienceInactiveStatus,
    judicialExperienceAppointmentDate,
    judicialExperienceActiveDate,
    judicialExperienceInactiveDate,
    judicialExperienceTermEndDate,
  }
"""

def import_judge_json(file_path):
    # load the deduped json and returns the judge json
    # i.e., { count: int, judges: { fullName: string, positions: Position }[] }
    json_data = open(file_path)
    deserialized = json.load(json_data)
    json_data.close()
    return deserialized
  
def get_middle_initial(last_first_middle_name, first_name, last_name):
  # lfm in format of Abbe, John Q. 
  # replace first and last names with empty strings
  # match and return initial values [A-Z\.]\.
  without_first = last_first_middle_name.replace(first_name, "")
  without_first_and_last = without_first.replace(last_name, "")

  # currently regex returns H.Q. or HQ. in addition to Q. 
  regex = re.compile('[A-Z\.]{1,3}\.$')
  match = regex.search(without_first_and_last)
  return match.group()

def does_person_exist(first, last, middle):
  # searches the database for an exact match on first, last, middle


def create_person_from_json(judgeJson):
  # extract fields, calculate fields, write to db
  # return person instance for associated data
  first_position = judgeJson['positions'][0]
  first = first_position['firstName']
  last = first_position['lastName']
  lfm = first_position['lastFirstMiddleName']
  # will be an empty string if not found
  # so save to write to db
  date_dod = first_position['deceasedDate']

  middle_init = get_middle_initial(lfm, first, last)

  person = Person(
    name_first=first
    name_last=last
    name_middle=middle_init
    date_dod=date_dod
  )

  person.save()
  return person


def create_position_from_json(person, positionJson):
  # extract data from the position Json
  job_title = positionJson['jobTitle']
  organization_name = positionJson['orgName']
  how_selected = get_how_selected(
    positionJson['judicialExperiencePendingStatus']
  )
  appointer = get_appointer(positionJson['judicialExperiencePendingSubType'])
  date_start = positionJson['experienceStatusEffectiveDate']

  position_type = positionJson['judicialPositionJobClass']

  # create the position instance
  position = Position(

  )

def get_how_selected(jud_exp_pending_status):
  if jud_exp_pending_status == 'Appointed':
    # SELECTION_METHODS['Appointment']
    # need to check to make sure legislatures don't appoint
    # returns 'a_gov' or 'a_legis'
    return 'a_gov'
  elfif jud_exp_pending_status == 'Elected':
    # SELECTION_METHODS['Election']
    # need to figure out if party or non-party
    # returns 'e_part' or 'e_non_part' 
    return 'e_non_part'

def get_appointer(jud_exp_pending_sub_type):
  # if type is 'Unknown' or 'Board of Supv' or 'Consolidation' or '(Blanks)' or 'Chief Judge'
  # it is an edge case
  edge_cases = ['Unknown', 'Board of Supv', 'Consolidation', '(Blanks)', 'Chief Judge']

  if (jud_exp_pending_sub_type in edge_cases):
    return ''

  return jud_exp_pending_sub_type

def get_termination_date_and_reason(experience_status, experience_status_effective_date):
