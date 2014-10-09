# -*- coding: utf-8 -*-

import os
import eyed3
import sys

execfile('/etc/courtlistener')
sys.path.append(INSTALL_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
from django.conf import settings

from alert.audio.models import Audio
from alert.lib.string_utils import anonymize, trunc
from alert.lib.mojibake import fix_mojibake
from alert.scrapers.models import ErrorLog
from alert.search.models import Document
from celery import task
from celery.task.sets import subtask
from citations.tasks import update_document_by_id
from django.core.files.base import ContentFile
from django.utils.encoding import smart_text, DjangoUnicodeDecodeError
from django.utils.timezone import now
from juriscraper.AbstractSite import logger
from lxml.html.clean import Cleaner
from lxml.etree import XMLSyntaxError
from seal_rookery import seals_data, seals_root

import glob
import subprocess
import time
import traceback

DEVNULL = open('/dev/null', 'w')


def get_clean_body_content(content):
    """Parse out the body from an html string, clean it up, and send it along.
    """
    cleaner = Cleaner(style=True,
                      remove_tags=['a', 'body', 'font', 'noscript'])
    try:
        return cleaner.clean_html(content)
    except XMLSyntaxError:
        return "Unable to extract the content from this file. Please try " \
               "reading the original."


def extract_from_doc(path, DEVNULL):
    """Extract text from docs.

    We use antiword to pull the text out of MS Doc files.
    """
    process = subprocess.Popen(['antiword', path, '-i', '1'], shell=False,
                               stdout=subprocess.PIPE, stderr=DEVNULL)
    content, err = process.communicate()
    return content, err


def extract_from_html(path):
    """Extract from html.

    A simple wrapper to go get content, and send it along.
    """
    try:
        content = open(path).read()
        content = get_clean_body_content(content)
        err = False
    except:
        content = ''
        err = True
    return content, err


def extract_from_pdf(doc, path, DEVNULL, callback=None):
    """ Extract text from pdfs.

    Here, we use pdftotext. If that fails, try to use tesseract under the
    assumption it's an image-based PDF. Once that is complete, we check for the
    letter e in our content. If it's not there, we try to fix the mojibake
    that ca9 sometimes creates.
    """
    process = subprocess.Popen(
        ["pdftotext", "-layout", "-enc", "UTF-8", path, "-"],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=DEVNULL
    )
    content, err = process.communicate()
    if content.strip() == '' and callback:
        # probably an image PDF. Send it to OCR
        result = subtask(callback).delay(path)
        success, content = result.get()
        if success:
            doc.extracted_by_ocr = True
        elif content == '' or not success:
            content = 'Unable to extract document content.'
    elif 'e' not in content:
        # It's a corrupt PDF from ca9. Fix it.
        content = fix_mojibake(unicode(content, 'utf-8', errors='ignore'))

    return doc, content, err


def extract_from_txt(path):
    """Extract text from plain text files: A fool's errand.

    Unfortunately, plain text files lack encoding information, so we have to
    guess. We could guess ascii, but we may as well use a superset of ascii,
    cp1252, and failing that try utf-8, ignoring errors. Most txt files we
    encounter were produced by converting wpd or doc files to txt on a
    Microsoft box, so assuming cp1252 as our first guess makes sense.

    May we hope for a better world.
    """
    try:
        err = False
        data = open(path).read()
        try:
            # Alas, cp1252 is probably still more popular than utf-8.
            content = smart_text(data, encoding='cp1252')
        except DjangoUnicodeDecodeError:
            content = smart_text(data, encoding='utf-8', errors='ignore')
    except:
        err = True
        content = ''
    return content, err


def extract_from_wpd(doc, path, DEVNULL):
    """Extract text from a Word Perfect file

    Yes, courts still use these, so we extract their text using wpd2html. Once
    that's done, we pull out the body of the HTML, and do some minor cleanup
    on it.
    """
    process = subprocess.Popen(['wpd2html', path], shell=False,
                               stdout=subprocess.PIPE, stderr=DEVNULL)
    content, err = process.communicate()

    content = get_clean_body_content(content)

    if 'not for publication' in content.lower():
        doc.precedential_status = "Unpublished"

    return doc, content, err


@task
def extract_doc_content(pk, callback=None, citation_countdown=0):
    """
    Given a document, we extract it, sniffing its extension, then store its
    contents in the database.  Finally, we asynchronously find citations in
    the document content and match them to other documents.

    TODO: this implementation cannot be distributed due to using local paths.
    """
    doc = Document.objects.get(pk=pk)

    path = str(doc.local_path)
    path = os.path.join(settings.MEDIA_ROOT, path)

    extension = path.split('.')[-1]
    if extension == 'doc':
        content, err = extract_from_doc(path, DEVNULL)
    elif extension == 'html':
        content, err = extract_from_html(path)
    elif extension == 'pdf':
        doc, content, err = extract_from_pdf(doc, path, DEVNULL, callback)
    elif extension == 'txt':
        content, err = extract_from_txt(path)
    elif extension == 'wpd':
        doc, content, err = extract_from_wpd(doc, path, DEVNULL)
    else:
        print ('*****Unable to extract content due to unknown extension: %s '
               'on doc: %s****' % (extension, doc))
        return 2

    if extension in ['html', 'wpd']:
        doc.html, blocked = anonymize(content)
    else:
        doc.plain_text, blocked = anonymize(content)

    if blocked:
        doc.blocked = True
        doc.date_blocked = now()

    if err:
        print "****Error extracting text from %s: %s****" % (extension, doc)
        return doc

    try:
        if citation_countdown == 0:
            doc.save(index=False)
        else:
            doc.save(index=True, commit=False)
    except Exception, e:
        print "****Error saving text to the db for: %s****" % doc
        print traceback.format_exc()
        return doc

    # Identify and link citations within the document content
    update_document_by_id.apply_async((doc.pk,), countdown=citation_countdown)

    return doc


def convert_to_tiff(path, tmp_file_prefix):
    image_magick_command = ['convert', '-depth', '4', '-density', '300',
                            '-background', 'white', '+matte', path,
                            '%s.tiff' % tmp_file_prefix]
    magick_out = subprocess.check_output(image_magick_command,
                                         stderr=subprocess.STDOUT)
    return magick_out


def convert_to_pngs(path, tmp_file_prefix):
    image_magick_command = ['convert', '-depth', '4', '-density', '300',
                            '-background', 'white', '+matte', path,
                            '%s.png' % tmp_file_prefix]
    magick_out = subprocess.check_output(image_magick_command,
                                         stderr=subprocess.STDOUT)
    return magick_out


def convert_to_txt(tmp_file_prefix, image_type):
    if image_type == 'tiffs':
        tesseract_command = ['tesseract', '%s.tiff' % tmp_file_prefix,
                             tmp_file_prefix, '-l', 'eng']
        tess_out = subprocess.check_output(tesseract_command,
                                           stderr=subprocess.STDOUT)
    elif image_type == 'pngs':
        for png in sorted(glob.glob('%s*' % tmp_file_prefix)):
            if 'tiff' not in png:
                tesseract_command = ['tesseract', png, png[:-4], '-l', 'eng']
                tess_out = subprocess.check_output(tesseract_command,
                                                   stderr=subprocess.STDOUT)
    return tess_out


@task
def extract_by_ocr(path):
    """Extract the contents of a PDF using OCR

    Convert the PDF to a tiff, then perform OCR on the tiff using Tesseract.
    Take the contents and the exit code and return them to the caller.
    """
    content = ''
    success = False
    image_type = 'tiffs'
    try:
        # The logic here is to try doing OCR with tiffs, and to fall back to
        # pngs if necessary. Depending on how each step goes, we either
        # proceed or abort.
        tmp_file_prefix = os.path.join('/tmp', str(time.time()))
        fail_msg = "Unable to extract the content from this file. Please try" \
                   " reading the original."
        try:
            convert_to_tiff(path, tmp_file_prefix)
        except subprocess.CalledProcessError:
            try:
                convert_to_pngs(path, tmp_file_prefix)
                image_type = 'pngs'
            except subprocess.CalledProcessError:
                content = fail_msg
                success = False

        try:
            convert_to_txt(tmp_file_prefix, image_type)
        except subprocess.CalledProcessError:
            if image_type == 'tiffs':
                # We haven't tried pngs yet, try them.
                try:
                    convert_to_pngs(path, tmp_file_prefix)
                    image_type = 'pngs'
                except subprocess.CalledProcessError:
                    # All is lost.
                    content = fail_msg
                    success = False
                try:
                    convert_to_txt(tmp_file_prefix, image_type)
                except subprocess.CalledProcessError:
                    # All is lost.
                    content = fail_msg
                    success = False

        try:
            if image_type == 'tiffs':
                content = open('%s.txt' % tmp_file_prefix).read()
            elif image_type == 'pngs':
                for txt_file in sorted(glob.glob('%s*' % tmp_file_prefix)):
                    if 'txt' in txt_file:
                        content += open(txt_file).read()
            success = True
        except IOError:
            print ("OCR was unable to finish due to not having a txt file "
                   "created. This usually happens when Tesseract cannot "
                   "ingest the tiff file at: %s" % path)
            content = fail_msg
            success = False

    finally:
        # Remove tmp_file and the text file
        for suffix in ['.tiff', '.txt']:
            try:
                os.remove(tmp_file_prefix + suffix)
            except OSError:
                pass

    return success, content


def set_mp3_meta_data(audio_obj, mp3_path):
    """Sets the meta data on the mp3 file to good values.

    :param audio_obj: an Audio object to clean up.
    :param mp3_path: the path to the mp3 to be converted.
    """
    court = audio_obj.docket.court

    # Nuke the old id3 tags.
    eyed3_command = [
        'eyeD3',
        '--remove-all',
        '--quiet',
        mp3_path,
    ]
    _ = subprocess.check_output(eyed3_command, stderr=subprocess.STDOUT)

    # Load the file, then create a fresh tag.
    audio_file = eyed3.load(mp3_path)
    audio_file.initTag()
    audio_file.tag.title = audio_obj.case_name
    audio_file.tag.album = u'{court}, {year}'.format(
        court=court.full_name,
        year=audio_obj.date_argued.year
    )
    audio_file.tag.artist = court.full_name
    audio_file.tag.artist_url = court.url
    audio_file.tag.audio_source_url = audio_obj.download_url
    audio_file.tag.comments.set(
        u'Argued: {date_argued}. Docket number: {docket_number}'.format(
            date_argued=audio_obj.date_argued.strftime('%Y-%m-%d'),
            docket_number=audio_obj.docket_number,
        ))
    audio_file.tag.genre = u'Speech'
    audio_file.tag.publisher = u'Free Law Project'
    audio_file.tag.publisher_url = u'http://www.freelawproject.org'
    audio_file.tag.recording_date = audio_obj.date_argued.strftime('%Y-%m-%d')

    # Add images to the mp3. If it has a seal, use that for the Front Cover
    # and use the FLP logo for the Publisher Logo. If it lacks a seal, use the
    # Publisher logo for both the front cover and the Publisher logo.
    try:
        has_seal = seals_data[court.pk]['has_seal']
    except AttributeError:
        # Unknown court in Seal Rookery.
        has_seal = False
    except KeyError:
        # Unknown court altogether (perhaps a test?)
        has_seal = False

    flp_image_frames = [
        3,   # "Front Cover". Complete list at eyed3/id3/frames.py
        14,  # "Publisher logo".
    ]
    if has_seal:
        with open(os.path.join(seals_root,
                               '512', '%s.png' % court.pk), 'r') as f:
            audio_file.tag.images.set(
                3,
                f.read(),
                'image/png',
                u'Seal for %s' % court.short_name,
            )
        flp_image_frames.remove(3)

    for frame in flp_image_frames:
        with open(os.path.join(settings.INSTALL_ROOT,
                               'alert', 'audio', 'static', 'png',
                               'producer.png'), 'r') as f:
            audio_file.tag.images.set(
                frame,
                f.read(),
                'image/png',
                u'Created for the public domain by Free Law Project',
            )

    audio_file.tag.save()


@task
def process_audio_file(pk):
    """Given the key to an audio file, extract its content and add the related
    meta data to the database.
    """
    audio_file = Audio.objects.get(pk=pk)
    path_to_original = audio_file.local_path_original_file.path

    path_to_tmp_location = os.path.join('/tmp', str(time.time()) + '.mp3')

    # Convert original file to:
    #  - mono (-ac 1)
    #  - sample rate (audio samples / s) of 22050Hz (-ar 22050)
    #  - constant bit rate (sample resolution) of 48kbps (-ab 48k)
    avconv_command = ['avconv', '-i', path_to_original,
                      '-ac', '1',
                      '-ar', '22050',
                      '-ab', '48k',
                      path_to_tmp_location]
    _ = subprocess.check_output(avconv_command, stderr=subprocess.STDOUT)

    # Have to do this last because otherwise the mp3 hasn't yet been generated.
    file_name = trunc(audio_file.case_name.lower(), 72) + '_cl.mp3'
    set_mp3_meta_data(audio_file, path_to_tmp_location)

    audio_file.duration = eyed3.load(path_to_tmp_location).info.time_secs

    with open(path_to_tmp_location, 'r') as mp3:
        try:
            cf = ContentFile(mp3.read())
            audio_file.local_path_mp3.save(file_name, cf, save=False)
        except:
            msg = "Unable to save mp3 to audio_file in scraper.tasks.process_" \
                  "audio_file for item: %s\nTraceback:\n%s" % \
                  (audio_file.pk, traceback.format_exc())
            logger.critical(msg)
            ErrorLog(log_level='CRITICAL', court=audio_file.docket.court,
                     message=msg).save()

    audio_file.processing_complete = True
    audio_file.save()
