import os
import time
from datetime import date
from django.test import TestCase
from cl.audio.models import Audio
from cl.lib import sunburnt
from cl.lib.solr_core_admin import create_solr_core, swap_solr_core, \
    delete_solr_core
from cl.scrapers.management.commands.cl_scrape_oral_arguments import \
    Command
from cl.scrapers.test_assets import test_opinion_scraper, \
    test_oral_arg_scraper
from cl.search.models import Court, Docket, Document
from django.conf import settings


class SolrTestCase(TestCase):
    """A generic class that contains the setUp and tearDown functions for
    inheriting children.

    Good for tests with both audio and documents.
    """
    fixtures = ['test_court.json']

    def setUp(self):
        # Set up some handy variables
        self.court = Court.objects.get(pk='test')

        # Set up testing cores in Solr and swap them in
        self.core_name_opinion = '%s.opinion-test-%s' % \
                                 (self.__module__, time.time())
        self.core_name_audio = '%s.audio-test-%s' % \
                               (self.__module__, time.time())
        create_solr_core(self.core_name_opinion)
        create_solr_core(
            self.core_name_audio,
            schema=os.path.join(settings.INSTALL_ROOT, 'Solr', 'conf',
                                'audio_schema.xml'),
            instance_dir='/usr/local/solr/example/solr/audio',
        )
        swap_solr_core('collection1', self.core_name_opinion)
        swap_solr_core('audio', self.core_name_audio)
        self.si_opinion = sunburnt.SolrInterface(
            settings.SOLR_OPINION_URL, mode='rw')
        self.si_audio = sunburnt.SolrInterface(
            settings.SOLR_AUDIO_URL, mode='rw')

        # Add three documents and three audio files to the index, but don't
        # extract their contents
        self.site_opinion = test_opinion_scraper.Site().parse()
        self.site_audio = test_oral_arg_scraper.Site().parse()
        cite_counts = (4, 6, 8)
        self.docs = {}
        for i in range(0, 3):
            docket = Docket(
                docket_number=self.site_opinion.docket_numbers[i],
                case_name=self.site_opinion.case_names[i],
                court=self.court,
            )
            docket.save()
            self.docs[i] = Document(
                case_name=self.site_opinion.case_names[i],
                neutral_cite=self.site_opinion.neutral_citations[i],
                federal_cite_one=self.site_opinion.west_citations[i],
                date_filed=self.site_opinion.case_dates[i],
                docket=docket,
                precedential_status=self.site_opinion.precedential_statuses[i],
                citation_count=cite_counts[i],
                nature_of_suit=self.site_opinion.nature_of_suit[i],
                judges=self.site_opinion.judges[i],
            )
            self.docs[i].save()

        # Create citations between the documents
        # 0 ---cites--> 1, 2
        # 1 ---cites--> 2
        # 2 ---cites--> 0
        self.docs[0].opinions_cited.add(self.docs[1])
        self.docs[0].opinions_cited.add(self.docs[2])
        self.docs[1].opinions_cited.add(self.docs[2])
        self.docs[2].opinions_cited.add(self.docs[0])

        for doc in self.docs.values():
            doc.save()

        # Scrape the audio "site" and add its contents
        site = test_oral_arg_scraper.Site().parse()
        Command().scrape_court(site, full_crawl=True)

        self.expected_num_results_opinion = 3
        self.expected_num_results_audio = 2
        self.si_opinion.commit()
        self.si_audio.commit()

    def tearDown(self):
        Document.objects.all().delete()
        Audio.objects.all().delete()
        swap_solr_core(self.core_name_opinion, 'collection1')
        swap_solr_core(self.core_name_audio, 'audio')
        delete_solr_core(self.core_name_opinion)
        delete_solr_core(self.core_name_audio)


class CitationTest(TestCase):
    """A simple class that abstracts out the creation and tear down of a few
    items with a simple citation relationship.
    """
    fixtures = ['test_court.json']

    def setUp(self):
        self.court = Court.objects.get(pk='test')

        # create 3 documents with their dockets
        docket1 = Docket(case_name=u"c1", court=self.court)
        docket2 = Docket(case_name=u"c2", court=self.court)
        docket3 = Docket(case_name=u"c3", court=self.court)
        docket1.save()
        docket2.save()
        docket3.save()
        d1 = Document(date_filed=date.today(), case_name=u"c1")
        d2 = Document(date_filed=date.today(), case_name=u"c2")
        d3 = Document(date_filed=date.today(), case_name=u"c3")
        d1.docket, d2.docket, d3.docket = docket1, docket2, docket3
        d1.save(index=False)
        d2.save(index=False)
        d3.save(index=False)

        # create simple citing relation: 1 cites 2 and 3; 2 cites 3; 3 cites 1;
        d1.opinions_cited.add(d2)
        d2.citation_count += 1
        d2.opinions_cited.add(d3)
        d3.citation_count += 1
        d3.opinions_cited.add(d1)
        d1.citation_count += 1
        d1.opinions_cited.add(d3)
        d3.citation_count += 1
        d1.save(index=False)
        d2.save(index=False)
        d3.save(index=False)

    def tearDown(self):
        Document.objects.all().delete()