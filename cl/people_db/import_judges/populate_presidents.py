# -*- coding: utf-8 -*-

import pandas as pd
import re
from datetime import date
from cl.people_db.models import Person, Position, Race, \
    PoliticalAffiliation, GRANULARITY_DAY, GRANULARITY_YEAR

def make_president(item, testing=False):
    """Takes the federal judge data <item> and associates it with a Judge object.
    Returns a Judge object.
    """

    
    date_dob = date(item['Born'].split('/'))
    dob_city = item['birth city'].strip()
    dob_state = item['birth state'].strip()
    
    date_dod, dod_city, dod_state = None, None, None
    if not pd.isnull(item['Died']):
        date_dod = date(item['Died'].split('/'))            
        dod_city = item['death city'].strip()
        dod_state = item['death state'].strip()

    if not pd.isnull(item['midname']):
        if len(item['midname']) == 1:
            item['midname'] = item['midname'] + '.'

    # instantiate Judge object
    person = Person(
            name_first=item['firstname'],
            name_middle=item['midname'],
            name_last=item['lastname'],
            gender='m',            
            cl_id=item['cl_id'],

            date_dob=date_dob,
            date_granularity_dob=GRANULARITY_DAY,
            dob_city=dob_city,
            dob_state=dob_state,
            date_dod=date_dod,
            date_granularity_dod=GRANULARITY_DAY,
            dod_city=dod_city,
            dod_state=dod_state,
            religion=item['Religion']
    )

    if not testing:
        person.save()

    if item['lastname'] == 'Obama':    
        race = Race.objects.get(race='b')
    else:
        race = Race.objects.get(race='w')
    person.race.add(race)        
    
    party = item['party'].lower()
    politics = PoliticalAffiliation(
            person=person,
            political_party=party,
            source='b'
    )
    if not testing:
        politics.save()

    position = Position(
            person=person,
            position_type='pres',

            date_start=item['term_start'],
            date_granularity_start=GRANULARITY_YEAR,
            date_termination=item['term_end'],
            date_granularity_termination=GRANULARITY_YEAR,
    )

    if not testing:
        position.save()

