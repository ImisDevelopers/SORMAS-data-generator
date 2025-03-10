import os
import random
from datetime import timedelta
import logging
import numpy as np
import pandas as pd
import sormas as sormas_api
from sormas import Disease, SymptomState, CaseOutcome, PresentCondition

from generator.cases import gen_case_dto
from generator.district import insert_district
from generator.event import gen_event_dto
from generator.location import gen_location_dto
from generator.person import gen_person_dto
from generator.region import insert_region, region_ref
from generator.symptoms import gen_symptom_dto
from generator.utils import sormas_db_connect
from universe.case import Case
from universe.contact import Contact
from universe.event import Event
from universe.event_participant import EventParticipant
from universe.infected import Infected
from universe.tick import Tick
from universe.util import export

random.seed(42)


class World:
    def __init__(self, beginning):
        self.today = Tick(beginning)
        self.history = list()
        self.model = self._load_model()
        sormas_domain = os.environ.get("DOMAIN_NAME")
        logging.info(f'SORMAS domain: {sormas_domain}')
        host = sormas_domain if sormas_domain else 'localhost'

        configuration = sormas_api.Configuration(
            host=f'http://{host}:6080/sormas-rest',
            username='SurvOff',
            password='SurvOff'
        )

        configuration.verify_ssl = False
        configuration.debug = True
        self.sormas_api_config = configuration

        self.regions = {}
        self.districts = {}

        with sormas_db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, uuid FROM region")
                res = cur.fetchone()
                self.regions[res[0]] = res[1]
                cur.execute("SELECT name, uuid FROM district")
                res = cur.fetchone()
                self.districts[res[0]] = res[1]

                # disable other diseases
                cur.execute("UPDATE diseaseconfiguration SET active = true WHERE disease = 'CORONAVIRUS'")
                cur.execute("UPDATE diseaseconfiguration SET active = false WHERE disease != 'CORONAVIRUS'")

    def _load_model(self):
        path = "../generator/data/out/"

        persons = pd.read_csv(path + "persons_df.csv")
        persons = persons.rename(columns={'id': 'id_person'})

        cases = persons.query('is_case == True')

        symptoms_cases = pd.read_csv(path + "symptoms_cases_df.csv")
        symptoms_cases = symptoms_cases.groupby('id_person').symptom.agg(','.join).reset_index()

        cases = pd.merge(cases, symptoms_cases, on='id_person')

        contacts = pd.read_csv(path + "contacts_df.csv")
        contacts = contacts.rename(columns={'id_contact': 'id_person'})
        contacts = pd.merge(contacts, persons, on='id_person')

        event_participants = pd.read_csv(path + "event_participants_df.csv")
        event_participants = event_participants.rename(columns={'id_participant': 'id_person'})
        event_participants = pd.merge(event_participants, persons, on='id_person')
        events = pd.read_csv(path + "events_df.csv")

        return {
            'cases': cases,
            'contacts': contacts,
            'events': events,
            'event_participants': event_participants
        }

    def _create_person(self, pers):
        from sormas import Sex
        date = pers.reporting_date
        birthdate_yyyy = None if type(date) is float else int(date.split('-')[0]) - pers.age

        lat = 0 if np.isnan(pers.latitude) else pers.latitude  # Fixme 0 condition
        lon = 0 if np.isnan(pers.longitude) else pers.longitude  # Fixme 0 condition
        location_dto = gen_location_dto(
            lat, lon,
            region_ref(self.regions['Voreingestellte Bundesländer']),
            self.districts['Voreingestellter Landkreis']
        )
        person = gen_person_dto(
            first_name=pers.first_name,
            last_name=pers.family_name,
            # todo other values like diverse
            sex=Sex.MALE if pers.sex == 'm' else Sex.FEMALE,
            birthdate_yyyy=birthdate_yyyy,
            address=location_dto,
            present_condition=PresentCondition.DEAD if pers.died else PresentCondition.ALIVE
        )
        # todo use Person class?
        return person

    def add_region(self, region):
        region_id = insert_region(region)
        self.regions[region] = region_id

    def add_district(self, district, region):
        region_id = self.regions[region]
        district_id = insert_district(district, region_id)
        self.districts[district] = district_id

    def pre_populate_susceptible(self, n=5):
        for _ in range(n):
            p = gen_person_dto()
            self.today.susceptible.append(p)

    def pre_populate_infected(self, n=5):
        for _ in range(n):
            person = gen_person_dto()
            case = Infected(person, Disease.CORONAVIRUS)
            self.today.infected.append(case)

    # todo Create individual cases that reproduce the
    #  aggregated data from covid dashboard and SurvStat
    def pre_populate_cases_and_contacts(self, n=5):
        def map_symptom(s):
            _map = {
                'Fieber': 'fever',
                'Husten': 'cough',
                'Geruchssinnsverlust': 'loss_of_smell'
            }
            res = {}
            for symptom in s.split(','):
                tmp = _map.get(symptom)
                if tmp is None:
                    logging.error(symptom + ' is not mapped')
                    continue
                res[tmp] = SymptomState.YES
            return res

        disease = Disease.CORONAVIRUS  # todo
        model_cases = self.model['cases']
        model_contacts = self.model['contacts']

        for i in range(n):
            m_case = model_cases.iloc[i]
            person_dto = self._create_person(m_case)
            symptoms_dto = gen_symptom_dto(Disease.CORONAVIRUS, map_symptom(m_case.symptom))

            case_outcome = CaseOutcome.DECEASED if m_case.died else CaseOutcome.NO_OUTCOME
            outcome_date = m_case.reporting_date  # FIXME

            case_dto = gen_case_dto(
                date=m_case.reporting_date,
                p_uuid=person_dto.uuid,
                disease=disease,
                symptoms=symptoms_dto,
                case_outcome=case_outcome,
                outcome_date=outcome_date
            )
            case = Case(person_dto, case_dto)
            self.today.cases.append(case)

            # create contacts for this case
            m_contacts = model_contacts.query(f'id_index == {m_case.id_person}')
            for m_contacts in m_contacts.iterrows():
                person_dto = self._create_person(m_contacts[1])
                contact = Contact(person_dto, case.inner.uuid, case.disease())
                self.today.contacts.append(contact)

    def pre_populate_events_and_participants(self, n=2):
        model_events = self.model['events']
        model_participants = self.model['event_participants']
        for i in range(n):
            m_event = model_events.iloc[i]
            import datetime
            # todo missing fields address longitude latitude
            start_date = datetime.date.today()  # m_event.date
            event_desc = 'hope1'  # random.choice(['Party', 'BBQ', 'Family meeting'])
            location_dto = gen_location_dto(
                m_event.latitude,
                m_event.longitude,
                region_ref(self.regions['Voreingestellte Bundesländer']),
                self.districts['Voreingestellter Landkreis']  # m_event.address]
            )
            # FIXME use the real place
            event_dto = gen_event_dto(event_desc, start_date, location_dto, Disease.CORONAVIRUS)

            # add participants
            m_participants = model_participants.query(f'id_event == {m_event.id}')
            participants = []
            for m_particpant in m_participants.iterrows():
                person = self._create_person(m_particpant[1])
                particpant = EventParticipant(person, event_dto.uuid)
                participants.append(particpant)
            self.today.events.append(Event(event_dto, participants))

    def pre_populate_infection_chains(self):
        raise NotImplementedError

    def start(self, disease=Disease.CORONAVIRUS):
        # todo needs rework regardin simuate and tick
        patient_zero = self.today.susceptible.pop()
        case_zero = Case(self.today.date, patient_zero, disease)
        self.today.infected.append(case_zero)
        # make the first tick

        today: Tick = self.today
        self.history.append(today)
        self.today = Tick(
            today.date + timedelta(days=1),
            susceptible=today.susceptible,
            infected=today.infected,
            removed=today.removed
        )

    def stop(self):
        self.history.append(self.today)

    def simulate(self, ticks=3):
        """
        Simulate the spread of the disease.
        :param ticks: For how many days the simulation should run.
        """
        for _ in range(ticks):
            self._tick()

    def export_sormas(self):
        export.sormas(self)

    def export_json(self):
        export.json(self)

    def _new_day(self):
        pass

    def _tick(self):
        """
        Make one day pass. Take the current state and evolve based on the predefined statistical values.
        """

        self.history.append(self.today)

        tomorrow = self.today
        tomorrow.date = self.today.date + timedelta(days=1)
        self.today = tomorrow
        # right now one person infects exactly one other persons and is removed
        # this will of course be changed to the correct values extracted from the live date

        today = self.today

        while today.cases:
            # get an infected person
            source_case = today.cases.pop()

            susceptible = today.susceptible.pop()
            new_case = Case(today.date, susceptible, source_case.disease())
            today.cases.append(new_case)
            today.removed.append(source_case)
