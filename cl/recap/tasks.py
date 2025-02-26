import logging
from tempfile import NamedTemporaryFile
from typing import List, Optional, Tuple
from zipfile import ZipFile

import requests
from celery import Task
from celery.canvas import chain
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils.timezone import now
from juriscraper.lib.exceptions import PacerLoginException, ParsingException
from juriscraper.lib.string_utils import CaseNameTweaker, harmonize
from juriscraper.pacer import (
    AppellateDocketReport,
    ClaimsRegister,
    DocketHistoryReport,
    DocketReport,
    PacerSession,
    PossibleCaseNumberApi,
)
from requests import HTTPError
from requests.packages.urllib3.exceptions import ReadTimeoutError

from cl.alerts.tasks import enqueue_docket_alert, send_docket_alert
from cl.celery_init import app
from cl.corpus_importer.tasks import (
    download_pacer_pdf_by_rd,
    get_attachment_page_by_rd,
    update_rd_metadata,
)
from cl.corpus_importer.utils import mark_ia_upload_needed
from cl.custom_filters.templatetags.text_filters import oxford_join
from cl.lib.crypto import sha1
from cl.lib.filesizes import convert_size_to_bytes
from cl.lib.pacer import map_cl_to_pacer_id
from cl.lib.pacer_session import get_pacer_cookie_from_cache
from cl.lib.recap_utils import get_document_filename
from cl.lib.string_diff import find_best_match
from cl.recap.mergers import (
    add_bankruptcy_data_to_docket,
    add_claims_to_docket,
    add_docket_entries,
    add_parties_and_attorneys,
    add_tags_to_objs,
    find_docket_object,
    get_data_from_att_report,
    merge_attachment_page_data,
    merge_pacer_docket_into_cl_docket,
    process_orphan_documents,
    update_docket_appellate_metadata,
    update_docket_metadata,
)
from cl.recap.models import (
    PROCESSING_STATUS,
    REQUEST_TYPE,
    UPLOAD_TYPE,
    FjcIntegratedDatabase,
    PacerFetchQueue,
    PacerHtmlFiles,
    ProcessingQueue,
)
from cl.scrapers.tasks import extract_recap_pdf, get_page_count
from cl.search.models import Docket, DocketEntry, RECAPDocument
from cl.search.tasks import add_items_to_solr, add_or_update_recap_docket

logger = logging.getLogger(__name__)
cnt = CaseNameTweaker()


def process_recap_upload(pq: ProcessingQueue) -> None:
    """Process an item uploaded from an extension or API user.

    Uploaded objects can take a variety of forms, and we'll need to
    process them accordingly.
    """
    if pq.upload_type == UPLOAD_TYPE.DOCKET:
        chain(
            process_recap_docket.s(pq.pk), add_or_update_recap_docket.s()
        ).apply_async()
    elif pq.upload_type == UPLOAD_TYPE.ATTACHMENT_PAGE:
        process_recap_attachment.delay(pq.pk)
    elif pq.upload_type == UPLOAD_TYPE.PDF:
        process_recap_pdf.delay(pq.pk)
    elif pq.upload_type == UPLOAD_TYPE.DOCKET_HISTORY_REPORT:
        chain(
            process_recap_docket_history_report.s(pq.pk),
            add_or_update_recap_docket.s(),
        ).apply_async()
    elif pq.upload_type == UPLOAD_TYPE.APPELLATE_DOCKET:
        chain(
            process_recap_appellate_docket.s(pq.pk),
            add_or_update_recap_docket.s(),
        ).apply_async()
    elif pq.upload_type == UPLOAD_TYPE.APPELLATE_ATTACHMENT_PAGE:
        process_recap_appellate_attachment.delay(pq.pk)
    elif pq.upload_type == UPLOAD_TYPE.CLAIMS_REGISTER:
        process_recap_claims_register.delay(pq.pk)
    elif pq.upload_type == UPLOAD_TYPE.DOCUMENT_ZIP:
        process_recap_zip.delay(pq.pk)


def do_pacer_fetch(fq):
    """Process a request made by a user to get an item from PACER.

    :param fq: The PacerFetchQueue item to process
    :return: None
    """
    result = None
    if fq.request_type == REQUEST_TYPE.DOCKET:
        # Request by docket_id
        c = chain(
            fetch_docket.si(fq.pk),
            add_or_update_recap_docket.s(),
            mark_fq_successful.si(fq.pk),
        )
        result = c.apply_async()
    elif fq.request_type == REQUEST_TYPE.PDF:
        # Request by recap_document_id
        rd_pk = fq.recap_document_id
        result = chain(
            fetch_pacer_doc_by_rd.si(rd_pk, fq.pk),
            extract_recap_pdf.si(rd_pk),
            add_items_to_solr.si([rd_pk], "search.RECAPDocument"),
            mark_fq_successful.si(fq.pk),
        ).apply_async()
    elif fq.request_type == REQUEST_TYPE.ATTACHMENT_PAGE:
        result = fetch_attachment_page.apply_async(args=(fq.pk,))
    return result


def mark_pq_successful(pq, d_id=None, de_id=None, rd_id=None):
    """Mark the processing queue item as successfully completed.

    :param pq: The ProcessingQueue object to manipulate
    :param d_id: The docket PK to associate with this upload. Either the docket
    that the RECAPDocument is associated with, or the docket that was uploaded.
    :param de_id: The docket entry to associate with this upload. Only applies
    to document uploads, which are associated with docket entries.
    :param rd_id: The RECAPDocument PK to associate with this upload. Only
    applies to document uploads (obviously).
    """
    # Ditch the original file
    pq.filepath_local.delete(save=False)
    if pq.debug:
        pq.error_message = "Successful debugging upload! Nice work."
    else:
        pq.error_message = "Successful upload! Nice work."
    pq.status = PROCESSING_STATUS.SUCCESSFUL
    pq.docket_id = d_id
    pq.docket_entry_id = de_id
    pq.recap_document_id = rd_id
    pq.save()
    return pq.status, pq.error_message


def mark_pq_status(pq, msg, status):
    """Mark the processing queue item as some process, and log the message.

    :param pq: The ProcessingQueue object to manipulate
    :param msg: The message to log and to save to pq's error_message field.
    :param status: A pq status code as defined on the ProcessingQueue model.
    """
    if msg:
        logger.info(msg)
    pq.error_message = msg
    pq.status = status
    pq.save()
    return pq.status, pq.error_message


@app.task(
    bind=True, max_retries=2, interval_start=5 * 60, interval_step=10 * 60
)
def process_recap_pdf(self, pk):
    """Process an uploaded PDF from the RECAP API endpoint.

    :param pk: The PK of the processing queue item you want to work on.
    :return: A RECAPDocument object that was created or updated.
    """
    """Save a RECAP PDF to the database."""
    pq = ProcessingQueue.objects.get(pk=pk)
    mark_pq_status(pq, "", PROCESSING_STATUS.IN_PROGRESS)

    if pq.attachment_number is None:
        document_type = RECAPDocument.PACER_DOCUMENT
    else:
        document_type = RECAPDocument.ATTACHMENT

    logger.info(f"Processing RECAP item (debug is: {pq.debug}): {pq} ")
    try:
        if pq.pacer_case_id:
            rd = RECAPDocument.objects.get(
                docket_entry__docket__pacer_case_id=pq.pacer_case_id,
                pacer_doc_id=pq.pacer_doc_id,
            )
        else:
            # Sometimes we don't have the case ID from PACER. Try to make this
            # work anyway.
            rd = RECAPDocument.objects.get(pacer_doc_id=pq.pacer_doc_id)
    except (RECAPDocument.DoesNotExist, RECAPDocument.MultipleObjectsReturned):
        try:
            d = Docket.objects.get(
                pacer_case_id=pq.pacer_case_id, court_id=pq.court_id
            )
        except Docket.DoesNotExist as exc:
            # No Docket and no RECAPDocument. Do a retry. Hopefully
            # the docket will be in place soon (it could be in a
            # different upload task that hasn't yet been processed).
            logger.warning(
                "Unable to find docket for processing queue '%s'. "
                "Retrying if max_retries is not exceeded." % pq
            )
            error_message = "Unable to find docket for item."
            if (self.request.retries == self.max_retries) or pq.debug:
                mark_pq_status(pq, error_message, PROCESSING_STATUS.FAILED)
                return None
            else:
                mark_pq_status(
                    pq, error_message, PROCESSING_STATUS.QUEUED_FOR_RETRY
                )
                raise self.retry(exc=exc)
        except Docket.MultipleObjectsReturned:
            msg = f"Too many dockets found when trying to save '{pq}'"
            mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
            return None

        # Got the Docket, attempt to get/create the DocketEntry, and then
        # create the RECAPDocument
        try:
            de = DocketEntry.objects.get(
                docket=d, entry_number=pq.document_number
            )
        except DocketEntry.DoesNotExist as exc:
            logger.warning(
                f"Unable to find docket entry for processing queue '{pq}'."
            )
            msg = "Unable to find docket entry for item."
            if (self.request.retries == self.max_retries) or pq.debug:
                mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
                return None
            else:
                mark_pq_status(pq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
                raise self.retry(exc=exc)
        else:
            # If we're here, we've got the docket and docket
            # entry, but were unable to find the document by
            # pacer_doc_id. This happens when pacer_doc_id is
            # missing, for example. ∴, try to get the document
            # from the docket entry.
            try:
                rd = RECAPDocument.objects.get(
                    docket_entry=de,
                    document_number=pq.document_number,
                    attachment_number=pq.attachment_number,
                    document_type=document_type,
                )
            except (
                RECAPDocument.DoesNotExist,
                RECAPDocument.MultipleObjectsReturned,
            ):
                # Unable to find it. Make a new item.
                rd = RECAPDocument(
                    docket_entry=de,
                    pacer_doc_id=pq.pacer_doc_id,
                    document_type=document_type,
                )

    rd.document_number = pq.document_number
    rd.attachment_number = pq.attachment_number

    # Do the file, finally.
    try:
        content = pq.filepath_local.read()
    except IOError as exc:
        msg = f"Internal processing error ({exc.errno}: {exc.strerror})."
        if (self.request.retries == self.max_retries) or pq.debug:
            mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
            return None
        else:
            mark_pq_status(pq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
            raise self.retry(exc=exc)

    new_sha1 = sha1(content)
    existing_document = all(
        [
            rd.sha1 == new_sha1,
            rd.is_available,
            rd.filepath_local,
        ]
    )
    if not existing_document:
        # Different sha1, it wasn't available, or it's missing from disk. Move
        # the new file over from the processing queue storage.
        cf = ContentFile(content)
        file_name = get_document_filename(
            rd.docket_entry.docket.court_id,
            rd.docket_entry.docket.pacer_case_id,
            rd.document_number,
            rd.attachment_number,
        )
        if not pq.debug:
            rd.filepath_local.save(file_name, cf, save=False)

            # Do page count and extraction
            extension = rd.filepath_local.name.split(".")[-1]
            with NamedTemporaryFile(
                prefix="rd_page_count_",
                suffix=f".{extension}",
                buffering=0,
            ) as tmp:
                tmp.write(content)
                rd.page_count = get_page_count(tmp.name, extension)
                rd.file_size = rd.filepath_local.size

        rd.ocr_status = None
        rd.is_available = True
        rd.sha1 = new_sha1
        rd.date_upload = now()

    if not pq.debug:
        try:
            rd.save()
        except (IntegrityError, ValidationError):
            msg = "Duplicate key on unique_together constraint"
            mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
            rd.filepath_local.delete(save=False)
            return None

    if not existing_document and not pq.debug:
        extract_recap_pdf(rd.pk)
        add_items_to_solr([rd.pk], "search.RECAPDocument")

    mark_pq_successful(
        pq,
        d_id=rd.docket_entry.docket_id,
        de_id=rd.docket_entry_id,
        rd_id=rd.pk,
    )
    mark_ia_upload_needed(rd.docket_entry.docket, save_docket=True)
    return rd


@app.task(bind=True, max_retries=5, ignore_result=True)
def process_recap_zip(self, pk):
    """Process a zip uploaded from a PACER district court

    The general process is to use our existing infrastructure. We open the zip,
    identify the documents inside, and then associate them with the rest of our
    collection.

    :param self: A celery task object
    :param pk: The PK of the ProcessingQueue object to process
    :return: A list of new PQ's that were created, one per PDF that was
    enqueued.
    """
    pq = ProcessingQueue.objects.get(pk=pk)
    mark_pq_status(pq, "", PROCESSING_STATUS.IN_PROGRESS)

    logger.info("Processing RECAP zip (debug is: %s): %s", pq.debug, pq)
    with ZipFile(pq.filepath_local.path, "r") as archive:
        # Security: Check for zip bombs.
        max_file_size = convert_size_to_bytes("200MB")
        for zip_info in archive.infolist():
            if zip_info.file_size < max_file_size:
                continue
            mark_pq_status(
                pq,
                "Zip too large; possible zip bomb. File in zip named %s "
                "would be %s bytes expanded."
                % (zip_info.filename, zip_info.file_size),
                PROCESSING_STATUS.INVALID_CONTENT,
            )
            return {"new_pqs": [], "tasks": []}

        # For each document in the zip, create a new PQ
        new_pqs = []
        tasks = []
        for file_name in archive.namelist():
            file_content = archive.read(file_name)
            f = SimpleUploadedFile(file_name, file_content)

            file_name = file_name.split(".pdf")[0]
            if "-" in file_name:
                doc_num, att_num = file_name.split("-")
                if att_num == "main":
                    att_num = None
            else:
                doc_num = file_name
                att_num = None

            if att_num:
                # An attachment, ∴ nuke the pacer_doc_id value, since it
                # corresponds to the main doc only.
                pacer_doc_id = ""
            else:
                pacer_doc_id = pq.pacer_doc_id

            # Create a new PQ and enqueue it for processing
            new_pq = ProcessingQueue.objects.create(
                court=pq.court,
                uploader=pq.uploader,
                pacer_case_id=pq.pacer_case_id,
                pacer_doc_id=pacer_doc_id,
                document_number=doc_num,
                attachment_number=att_num,
                filepath_local=f,
                status=PROCESSING_STATUS.ENQUEUED,
                upload_type=UPLOAD_TYPE.PDF,
                debug=pq.debug,
            )
            new_pqs.append(new_pq.pk)
            tasks.append(process_recap_pdf.delay(new_pq.pk))

        # At the end, mark the pq as successful and return the PQ
        mark_pq_status(
            pq,
            f"Successfully created ProcessingQueue objects: {oxford_join(new_pqs)}",
            PROCESSING_STATUS.SUCCESSFUL,
        )

        # Returning the tasks allows tests to wait() for the PDFs to complete
        # before checking assertions.
        return {
            "new_pqs": new_pqs,
            "tasks": tasks,
        }


@app.task(bind=True, max_retries=5, ignore_result=True)
def process_recap_docket(self, pk):
    """Process an uploaded docket from the RECAP API endpoint.

    :param pk: The primary key of the processing queue item you want to work
    on.
    :returns: A dict of the form:

        {
            // The PK of the docket that's created or updated
            'docket_pk': 22,
            // A boolean indicating whether a new docket entry or
            // recap document was created (implying a Solr needs
            // updating).
            'content_updated': True,
        }

    This value is a dict so that it can be ingested in a Celery chain.

    """
    start_time = now()
    pq = ProcessingQueue.objects.get(pk=pk)
    mark_pq_status(pq, "", PROCESSING_STATUS.IN_PROGRESS)
    logger.info(f"Processing RECAP item (debug is: {pq.debug}): {pq}")

    report = DocketReport(map_cl_to_pacer_id(pq.court_id))

    try:
        text = pq.filepath_local.read().decode()
    except IOError as exc:
        msg = f"Internal processing error ({exc.errno}: {exc.strerror})."
        if (self.request.retries == self.max_retries) or pq.debug:
            mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
            return None
        else:
            mark_pq_status(pq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
            raise self.retry(exc=exc)

    if "History/Documents" in text:
        # Prior to 1.1.8, we did not separate docket history reports into their
        # own upload_type. Alas, we still have some old clients around, so we
        # need to handle those clients here.
        pq.upload_type = UPLOAD_TYPE.DOCKET_HISTORY_REPORT
        pq.save()
        process_recap_docket_history_report(pk)
        self.request.chain = None
        return None

    report._parse_text(text)
    data = report.data
    logger.info(f"Parsing completed of item {pq}")

    if data == {}:
        # Not really a docket. Some sort of invalid document (see Juriscraper).
        msg = "Not a valid docket upload."
        mark_pq_status(pq, msg, PROCESSING_STATUS.INVALID_CONTENT)
        self.request.chain = None
        return None

    # Merge the contents of the docket into CL.
    d = find_docket_object(
        pq.court_id, pq.pacer_case_id, data["docket_number"]
    )

    d.add_recap_source()
    update_docket_metadata(d, data)
    if not d.pacer_case_id:
        d.pacer_case_id = pq.pacer_case_id

    if pq.debug:
        mark_pq_successful(pq, d_id=d.pk)
        self.request.chain = None
        return {"docket_pk": d.pk, "content_updated": False}

    d.save()

    # Add the HTML to the docket in case we need it someday.
    pacer_file = PacerHtmlFiles(
        content_object=d, upload_type=UPLOAD_TYPE.DOCKET
    )
    pacer_file.filepath.save(
        "docket.html",  # We only care about the ext w/UUIDFileSystemStorage
        ContentFile(text),
    )

    rds_created, content_updated = add_docket_entries(
        d, data["docket_entries"]
    )
    add_parties_and_attorneys(d, data["parties"])
    process_orphan_documents(rds_created, pq.court_id, d.date_filed)
    if content_updated:
        newly_enqueued = enqueue_docket_alert(d.pk)
        if newly_enqueued:
            send_docket_alert(d.pk, start_time)
    mark_pq_successful(pq, d_id=d.pk)
    return {
        "docket_pk": d.pk,
        "content_updated": bool(rds_created or content_updated),
    }


@app.task(
    bind=True, max_retries=3, interval_start=5 * 60, interval_step=5 * 60
)
def process_recap_attachment(
    self: Task,
    pk: int,
    tag_names: Optional[List[str]] = None,
) -> Optional[Tuple[int, str]]:
    """Process an uploaded attachment page from the RECAP API endpoint.

    :param self: The Celery teask
    :param pk: The primary key of the processing queue item you want to work on
    :param tag_names: A list of tag names to add to all items created or
    modified in this function.
    :return: Tuple indicating the status of the processing and a related
    message
    """
    pq = ProcessingQueue.objects.get(pk=pk)
    mark_pq_status(pq, "", PROCESSING_STATUS.IN_PROGRESS)
    logger.info(f"Processing RECAP item (debug is: {pq.debug}): {pq}")

    try:
        text = pq.filepath_local.read().decode()
    except IOError as exc:
        msg = f"Internal processing error ({exc.errno}: {exc.strerror})."
        if (self.request.retries == self.max_retries) or pq.debug:
            mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
            return None
        else:
            mark_pq_status(pq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
            raise self.retry(exc=exc)

    att_data = get_data_from_att_report(text, pq.court_id)
    logger.info(f"Parsing completed for item {pq}")

    if att_data == {}:
        # Bad attachment page.
        msg = "Not a valid attachment page upload."
        self.request.chain = None
        return mark_pq_status(pq, msg, PROCESSING_STATUS.INVALID_CONTENT)

    if pq.pacer_case_id in ["undefined", "null"]:
        # Bad data from the client. Fix it with parsed data.
        pq.pacer_case_id = att_data.get("pacer_case_id")
        pq.save()

    try:
        rds_affected, de = merge_attachment_page_data(
            pq.court,
            pq.pacer_case_id,
            att_data["pacer_doc_id"],
            att_data["document_number"],
            text,
            att_data["attachments"],
            pq.debug,
        )
    except RECAPDocument.MultipleObjectsReturned:
        msg = (
            "Too many documents found when attempting to associate "
            "attachment data"
        )
        return mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
    except RECAPDocument.DoesNotExist as exc:
        msg = "Could not find docket to associate with attachment metadata"
        if (self.request.retries == self.max_retries) or pq.debug:
            return mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
        else:
            mark_pq_status(pq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
            raise self.retry(exc=exc)

    add_tags_to_objs(tag_names, rds_affected)
    return mark_pq_successful(pq, d_id=de.docket_id, de_id=de.pk)


@app.task(
    bind=True, max_retries=3, interval_start=5 * 60, interval_step=5 * 60
)
def process_recap_claims_register(self, pk):
    """Merge bankruptcy claims registry HTML into RECAP

    :param pk: The primary key of the processing queue item you want to work on
    :type pk: int
    :return: None
    :rtype: None
    """
    pq = ProcessingQueue.objects.get(pk=pk)
    if pq.debug:
        # Proper debugging not supported on this endpoint. Just abort.
        mark_pq_successful(pq)
        self.request.chain = None
        return None

    mark_pq_status(pq, "", PROCESSING_STATUS.IN_PROGRESS)
    logger.info(f"Processing RECAP item (debug is: {pq.debug}): {pq}")

    try:
        text = pq.filepath_local.read().decode()
    except IOError as exc:
        msg = f"Internal processing error ({exc.errno}: {exc.strerror})."
        if (self.request.retries == self.max_retries) or pq.debug:
            mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
            return None
        else:
            mark_pq_status(pq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
            raise self.retry(exc=exc)

    report = ClaimsRegister(map_cl_to_pacer_id(pq.court_id))
    report._parse_text(text)
    data = report.data
    logger.info(f"Parsing completed for item {pq}")

    if not data:
        # Bad HTML
        msg = "Not a valid claims registry page or other parsing failure"
        mark_pq_status(pq, msg, PROCESSING_STATUS.INVALID_CONTENT)
        self.request.chain = None
        return None

    # Merge the contents of the docket into CL.
    d = find_docket_object(
        pq.court_id, pq.pacer_case_id, data["docket_number"]
    )

    # Merge the contents into CL
    d.add_recap_source()
    update_docket_metadata(d, data)

    try:
        d.save()
    except IntegrityError as exc:
        logger.warning(
            "Race condition experienced while attempting docket save."
        )
        error_message = "Unable to save docket due to IntegrityError."
        if self.request.retries == self.max_retries:
            mark_pq_status(pq, error_message, PROCESSING_STATUS.FAILED)
            self.request.chain = None
            return None
        else:
            mark_pq_status(
                pq, error_message, PROCESSING_STATUS.QUEUED_FOR_RETRY
            )
            raise self.retry(exc=exc)

    add_bankruptcy_data_to_docket(d, data)
    add_claims_to_docket(d, data["claims"])
    logger.info("Created/updated claims data for %s", pq)

    # Add the HTML to the docket in case we need it someday.
    pacer_file = PacerHtmlFiles(
        content_object=d, upload_type=UPLOAD_TYPE.CLAIMS_REGISTER
    )
    pacer_file.filepath.save(
        # We only care about the ext w/UUIDFileSystemStorage
        "claims_registry.html",
        ContentFile(text),
    )

    mark_pq_successful(pq, d_id=d.pk)
    return {"docket_pk": d.pk}


@app.task(
    bind=True, max_retries=3, interval_start=5 * 60, interval_step=5 * 60
)
def process_recap_docket_history_report(self, pk):

    """Process the docket history report.

    :param pk: The primary key of the processing queue item you want to work on
    :returns: A dict indicating whether the docket needs Solr re-indexing.
    """
    start_time = now()
    pq = ProcessingQueue.objects.get(pk=pk)
    mark_pq_status(pq, "", PROCESSING_STATUS.IN_PROGRESS)
    logger.info(f"Processing RECAP item (debug is: {pq.debug}): {pq}")

    try:
        text = pq.filepath_local.read().decode()
    except IOError as exc:
        msg = f"Internal processing error ({exc.errno}: {exc.strerror})."
        if (self.request.retries == self.max_retries) or pq.debug:
            mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
            return None
        else:
            mark_pq_status(pq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
            raise self.retry(exc=exc)

    report = DocketHistoryReport(map_cl_to_pacer_id(pq.court_id))
    report._parse_text(text)
    data = report.data
    logger.info(f"Parsing completed for item {pq}")

    if data == {}:
        # Bad docket history page.
        msg = "Not a valid docket history page upload."
        mark_pq_status(pq, msg, PROCESSING_STATUS.INVALID_CONTENT)
        self.request.chain = None
        return None

    # Merge the contents of the docket into CL.
    d = find_docket_object(
        pq.court_id, pq.pacer_case_id, data["docket_number"]
    )

    d.add_recap_source()
    update_docket_metadata(d, data)

    if pq.debug:
        mark_pq_successful(pq, d_id=d.pk)
        self.request.chain = None
        return {"docket_pk": d.pk, "content_updated": False}

    try:
        d.save()
    except IntegrityError as exc:
        logger.warning(
            "Race condition experienced while attempting docket save."
        )
        error_message = "Unable to save docket due to IntegrityError."
        if self.request.retries == self.max_retries:
            mark_pq_status(pq, error_message, PROCESSING_STATUS.FAILED)
            self.request.chain = None
            return None
        else:
            mark_pq_status(
                pq, error_message, PROCESSING_STATUS.QUEUED_FOR_RETRY
            )
            raise self.retry(exc=exc)

    # Add the HTML to the docket in case we need it someday.
    pacer_file = PacerHtmlFiles(
        content_object=d, upload_type=UPLOAD_TYPE.DOCKET_HISTORY_REPORT
    )
    pacer_file.filepath.save(
        # We only care about the ext w/UUIDFileSystemStorage
        "docket_history.html",
        ContentFile(text),
    )

    rds_created, content_updated = add_docket_entries(
        d, data["docket_entries"]
    )
    process_orphan_documents(rds_created, pq.court_id, d.date_filed)
    if content_updated:
        newly_enqueued = enqueue_docket_alert(d.pk)
        if newly_enqueued:
            send_docket_alert(d.pk, start_time)
    mark_pq_successful(pq, d_id=d.pk)
    return {
        "docket_pk": d.pk,
        "content_updated": bool(rds_created or content_updated),
    }


@app.task(bind=True, max_retries=3, ignore_result=True)
def process_recap_appellate_docket(self, pk):
    """Process an uploaded appellate docket from the RECAP API endpoint.

    :param pk: The primary key of the processing queue item you want to work
    on.
    :returns: A dict of the form:

        {
            // The PK of the docket that's created or updated
            'docket_pk': 22,
            // A boolean indicating whether a new docket entry or
            // recap document was created (implying a Solr needs
            // updating).
            'content_updated': True,
        }

    This value is a dict so that it can be ingested in a Celery chain.

    """
    start_time = now()
    pq = ProcessingQueue.objects.get(pk=pk)
    mark_pq_status(pq, "", PROCESSING_STATUS.IN_PROGRESS)
    logger.info(
        f"Processing Appellate RECAP item (debug is: {pq.debug}): {pq}"
    )

    report = AppellateDocketReport(map_cl_to_pacer_id(pq.court_id))

    try:
        text = pq.filepath_local.read().decode()
    except IOError as exc:
        msg = f"Internal processing error ({exc.errno}: {exc.strerror})."
        if (self.request.retries == self.max_retries) or pq.debug:
            mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
            return None
        else:
            mark_pq_status(pq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
            raise self.retry(exc=exc)

    report._parse_text(text)
    data = report.data
    logger.info(f"Parsing completed of item {pq}")

    if data == {}:
        # Not really a docket. Some sort of invalid document (see Juriscraper).
        msg = "Not a valid docket upload."
        mark_pq_status(pq, msg, PROCESSING_STATUS.INVALID_CONTENT)
        self.request.chain = None
        return None

    # Merge the contents of the docket into CL.
    d = find_docket_object(
        pq.court_id, pq.pacer_case_id, data["docket_number"]
    )

    d.add_recap_source()
    update_docket_metadata(d, data)
    d, og_info = update_docket_appellate_metadata(d, data)
    if not d.pacer_case_id:
        d.pacer_case_id = pq.pacer_case_id

    if pq.debug:
        mark_pq_successful(pq, d_id=d.pk)
        self.request.chain = None
        return {"docket_pk": d.pk, "content_updated": False}

    if og_info is not None:
        og_info.save()
        d.originating_court_information = og_info
    d.save()

    # Add the HTML to the docket in case we need it someday.
    pacer_file = PacerHtmlFiles(
        content_object=d, upload_type=UPLOAD_TYPE.APPELLATE_DOCKET
    )
    pacer_file.filepath.save(
        "docket.html",  # We only care about the ext w/UUIDFileSystemStorage
        ContentFile(text),
    )

    rds_created, content_updated = add_docket_entries(
        d, data["docket_entries"]
    )
    add_parties_and_attorneys(d, data["parties"])
    process_orphan_documents(rds_created, pq.court_id, d.date_filed)
    if content_updated:
        newly_enqueued = enqueue_docket_alert(d.pk)
        if newly_enqueued:
            send_docket_alert(d.pk, start_time)
    mark_pq_successful(pq, d_id=d.pk)
    return {
        "docket_pk": d.pk,
        "content_updated": bool(rds_created or content_updated),
    }


@app.task(
    bind=True, max_retries=3, interval_start=5 * 60, interval_step=5 * 60
)
def process_recap_appellate_attachment(self, pk):
    """Process the appellate attachment pages.

    For now, this is a stub until we can get the parser working properly in
    Juriscraper.
    """
    pq = ProcessingQueue.objects.get(pk=pk)
    msg = "Appellate attachment pages not yet supported. Coming soon."
    mark_pq_status(pq, msg, PROCESSING_STATUS.FAILED)
    return None


@app.task
def create_new_docket_from_idb(idb_row):
    """Create a new docket for the IDB item found. Populate it with all
    applicable fields.

    :param idb_row: An FjcIntegratedDatabase object with which to create a
    Docket.
    :return Docket: The created Docket object.
    """
    case_name = f"{idb_row.plaintiff} v. {idb_row.defendant}"
    d = Docket(
        source=Docket.IDB,
        court=idb_row.district,
        idb_data=idb_row,
        date_filed=idb_row.date_filed,
        date_terminated=idb_row.date_terminated,
        case_name=case_name,
        case_name_short=cnt.make_case_name_short(case_name),
        docket_number_core=idb_row.docket_number,
        nature_of_suit=idb_row.get_nature_of_suit_display(),
        jurisdiction_type=idb_row.get_jurisdiction_display() or "",
    )
    try:
        d.save()
    except IntegrityError:
        # Happens when the IDB row is already associated with a docket. Remove
        # the other association and try again.
        Docket.objects.filter(idb_data=idb_row).update(
            date_modified=now(), idb_data=None
        )
        d.save()

    logger.info("Created docket %s for IDB row: %s", d.pk, idb_row)
    return d.pk


@app.task
def merge_docket_with_idb(d, idb_row):
    """Merge an existing docket with an idb_row.

    :param d: A Docket object pk to update.
    :param idb_row: A FjcIntegratedDatabase object to use as a source for
    updates.
    :return None
    """
    d.add_idb_source()
    d.idb_data = idb_row
    d.date_filed = d.date_filed or idb_row.date_filed
    d.date_terminated = d.date_terminated or idb_row.date_terminated
    d.nature_of_suit = d.nature_of_suit or idb_row.get_nature_of_suit_display()
    d.jurisdiction_type = (
        d.jurisdiction_type or idb_row.get_jurisdiction_display()
    )
    try:
        d.save()
    except IntegrityError:
        # Happens when the IDB row is already associated with a docket. Remove
        # the other association and try again.
        Docket.objects.filter(idb_data=idb_row).update(
            date_modified=now(), idb_data=None
        )
        d.save()


def do_heuristic_match(idb_row, ds):
    """Use cosine similarity of case names from the IDB to try to find a match
    out of several possibilities in the DB.

    :param idb_row: The FJC IDB row to match against
    :param ds: A list of Dockets that might match
    :returns: The best-matching Docket in ds if possible, else None
    """
    case_names = []
    for d in ds:
        case_name = harmonize(d.case_name)
        parts = case_name.lower().split(" v. ")
        if len(parts) == 1:
            case_names.append(case_name)
        elif len(parts) == 2:
            plaintiff, defendant = parts[0], parts[1]
            case_names.append(f"{plaintiff[0:30]} v. {defendant[0:30]}")
        elif len(parts) > 2:
            case_names.append(case_name)
    idb_case_name = harmonize(f"{idb_row.plaintiff} v. {idb_row.defendant}")
    results = find_best_match(case_names, idb_case_name, case_sensitive=False)
    if results["ratio"] > 0.65:
        logger.info(
            "Found good match by case name for %s: %s",
            idb_case_name,
            results["match_str"],
        )
        d = ds[results["match_index"]]
    else:
        logger.info(
            "No good match after office and case name filtering. Creating "
            "new item: %s",
            idb_row,
        )
        d = None
    return d


@app.task
def create_or_merge_from_idb_chunk(idb_chunk):
    """Take a chunk of IDB rows and either merge them into the Docket table or
    create new items for them in the docket table.

    :param idb_chunk: A list of FjcIntegratedDatabase PKs
    :type idb_chunk: list
    :return: None
    :rtype: None
    """
    for idb_pk in idb_chunk:
        idb_row = FjcIntegratedDatabase.objects.get(pk=idb_pk)
        ds = (
            Docket.objects.filter(
                docket_number_core=idb_row.docket_number,
                court=idb_row.district,
            )
            .exclude(docket_number__icontains="cr")
            .exclude(case_name__icontains="sealed")
            .exclude(case_name__icontains="suppressed")
            .exclude(case_name__icontains="search warrant")
        )
        count = ds.count()
        if count == 0:
            msg = "Creating new docket for IDB row: %s"
            logger.info(msg, idb_row)
            create_new_docket_from_idb(idb_row)
            continue
        elif count == 1:
            d = ds[0]
            msg = "Merging Docket %s with IDB row: %s"
            logger.info(msg, d, idb_row)
            merge_docket_with_idb(d, idb_row)
            continue

        msg = "Unable to merge. Got %s dockets for row: %s"
        logger.info(msg, count, idb_row)

        d = do_heuristic_match(idb_row, ds)
        if d is not None:
            merge_docket_with_idb(d, idb_row)
        else:
            create_new_docket_from_idb(idb_row)


@app.task
def update_docket_from_hidden_api(data):
    """Update the docket based on the result of a lookup in the hidden API.

    :param data: A dict as returned by get_pacer_case_id_and_title
    or None if looking up the item failed.
    :return None
    """
    if data is None:
        return None

    d = Docket.objects.get(pk=data["pass_through"])
    d.docket_number = data["docket_number"]
    d.pacer_case_id = data["pacer_case_id"]
    try:
        d.save()
    except IntegrityError:
        # This is a difficult spot. The IDB data has cases that are not in
        # PACER. For example, in IDB there are two rows for 6:92-cv-657 in
        # oked, but in PACER, there is just one. In IDB the two rows *are*
        # distinct, with different filing dates, for example. So what happens
        # is, we try to find the docket for the first one, get none, and start
        # creating it. Meanwhile, via a race condition, we try to get the
        # second one, fail, and then start creating *it*. The first finishes,
        # then the second tries to lookup the pacer_case_id. Unfortunately, b/c
        # there's only one item in PACER for the docket number looked up, that
        # is returned, and we get an integrity error since we can't have the
        # same pacer_case_id, docket_number pair in a single court. Solution?
        # Delete the second one, which was created via race condition, and
        # shouldn't have existed anyway.
        d.delete()


@app.task(
    bind=True,
    max_retries=3,
    interval_start=5,
    interval_step=5,
    ignore_result=True,
)
@transaction.atomic
def fetch_pacer_doc_by_rd(self, rd_pk: int, fq_pk: int) -> Optional[int]:
    """Fetch a PACER PDF by rd_pk

    This is very similar to get_pacer_doc_by_rd, except that it manages
    status as it proceeds and it gets the cookie info from redis.

    :param rd_pk: The PK of the RECAP Document to get.
    :param fq_pk: The PK of the RECAP Fetch Queue to update.
    :return: The RECAPDocument PK
    """
    rd = RECAPDocument.objects.get(pk=rd_pk)
    fq = PacerFetchQueue.objects.get(pk=fq_pk)
    mark_fq_status(fq, "", PROCESSING_STATUS.IN_PROGRESS)

    if rd.is_available:
        msg = "PDF already marked as 'is_available'. Doing nothing."
        mark_fq_status(fq, msg, PROCESSING_STATUS.SUCCESSFUL)
        self.request.chain = None
        return

    if not rd.pacer_doc_id:
        msg = (
            "Missing 'pacer_doc_id' attribute. Without this attribute we "
            "cannot identify the document properly. Missing pacer_doc_id "
            "attributes usually indicate that the item may not have a "
            "document associated with it, or it may need to be updated via "
            "the docket report to acquire a pacer_doc_id. Aborting request."
        )
        mark_fq_status(fq, msg, PROCESSING_STATUS.INVALID_CONTENT)
        self.request.chain = None
        return

    cookies = get_pacer_cookie_from_cache(fq.user_id)
    if not cookies:
        msg = "Unable to find cached cookies. Aborting request."
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        self.request.chain = None
        return

    pacer_case_id = rd.docket_entry.docket.pacer_case_id
    try:
        r = download_pacer_pdf_by_rd(
            rd.pk, pacer_case_id, rd.pacer_doc_id, cookies
        )
    except (requests.RequestException, HTTPError):
        msg = "Failed to get PDF from network."
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        self.request.chain = None
        return

    court_id = rd.docket_entry.docket.court_id
    success, msg = update_rd_metadata(
        self,
        rd_pk,
        r,
        court_id,
        pacer_case_id,
        rd.pacer_doc_id,
        rd.document_number,
        rd.attachment_number,
    )

    if success is False:
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        self.request.chain = None
        return

    return rd.pk


@app.task(
    bind=True,
    max_retries=3,
    interval_start=5,
    interval_step=5,
    ignore_result=True,
)
@transaction.atomic
def fetch_attachment_page(self: Task, fq_pk: int) -> None:
    """Fetch a PACER attachment page by rd_pk

    This is very similar to process_recap_attachment, except that it manages
    status as it proceeds and it gets the cookie info from redis.

    :param self: The celery task
    :param fq_pk: The PK of the RECAP Fetch Queue to update.
    :return: None
    """
    fq = PacerFetchQueue.objects.get(pk=fq_pk)
    mark_fq_status(fq, "", PROCESSING_STATUS.IN_PROGRESS)

    rd = fq.recap_document
    if not rd.pacer_doc_id:
        msg = (
            "Unable to get attachment page: Unknown pacer_doc_id for "
            "RECAP Document object %s" % rd.pk
        )
        mark_fq_status(fq, msg, PROCESSING_STATUS.NEEDS_INFO)
        return

    cookies = get_pacer_cookie_from_cache(fq.user_id)
    if not cookies:
        msg = "Unable to find cached cookies. Aborting request."
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        return

    try:
        r = get_attachment_page_by_rd(rd.pk, cookies)
    except (requests.RequestException, HTTPError):
        msg = "Failed to get attachment page from network."
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        return

    text = r.response.text
    att_data = get_data_from_att_report(text, rd.docket_entry.docket.court_id)

    if att_data == {}:
        msg = "Not a valid attachment page upload"
        mark_fq_status(fq, msg, PROCESSING_STATUS.INVALID_CONTENT)
        return

    try:
        merge_attachment_page_data(
            rd.docket_entry.docket.court,
            rd.docket_entry.docket.pacer_case_id,
            att_data["pacer_doc_id"],
            att_data["document_number"],
            text,
            att_data["attachments"],
        )
    except RECAPDocument.MultipleObjectsReturned:
        msg = (
            "Too many documents found when attempting to associate "
            "attachment data"
        )
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        return
    except RECAPDocument.DoesNotExist as exc:
        msg = "Could not find docket to associate with attachment metadata"
        if self.request.retries == self.max_retries:
            mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
            return
        mark_fq_status(fq, msg, PROCESSING_STATUS.QUEUED_FOR_RETRY)
        raise self.retry(exc=exc)
    msg = "Successfully completed fetch and save."
    mark_fq_status(fq, msg, PROCESSING_STATUS.SUCCESSFUL)


def get_fq_docket_kwargs(fq):
    """Gather the kwargs for the Juriscraper DocketReport from the fq object

    :param fq: The PacerFetchQueue object
    :return: A dict of the kwargs we can send to the DocketReport
    """
    return {
        "doc_num_start": fq.de_number_start,
        "doc_num_end": fq.de_number_end,
        "date_start": fq.de_date_start,
        "date_end": fq.de_date_end,
        "show_parties_and_counsel": fq.show_parties_and_counsel,
        "show_terminated_parties": fq.show_terminated_parties,
        "show_list_of_member_cases": fq.show_list_of_member_cases,
    }


def fetch_pacer_case_id_and_title(s, fq, court_id):
    """Use PACER's hidden API to learn the pacer_case_id of a case

    :param s: A PacerSession object to use
    :param fq: The PacerFetchQueue object to use
    :param court_id: The CL ID of the court
    :return: A dict of the new information or an empty dict if it fails
    """
    if (fq.docket_id and not fq.docket.pacer_case_id) or fq.docket_number:
        # We lack the pacer_case_id either on the docket or from the
        # submission. Look it up.
        docket_number = fq.docket_number or getattr(
            fq.docket, "docket_number", None
        )
        report = PossibleCaseNumberApi(map_cl_to_pacer_id(court_id), s)
        report.query(docket_number)
        return report.data()
    return {}


def fetch_docket_by_pacer_case_id(session, court_id, pacer_case_id, fq):
    """Download the docket from PACER and merge it into CL

    :param session: A PacerSession object to work with
    :param court_id: The CL ID of the court
    :param pacer_case_id: The pacer_case_id of the docket, if known
    :param fq: The PacerFetchQueue object
    :return: a dict with information about the docket and the new data
    """
    report = DocketReport(map_cl_to_pacer_id(court_id), session)
    report.query(pacer_case_id, **get_fq_docket_kwargs(fq))

    docket_data = report.data
    if not docket_data:
        raise ParsingException("No data found in docket report.")
    if fq.docket_id:
        d = Docket.objects.get(pk=fq.docket_id)
    else:
        d = find_docket_object(
            court_id, pacer_case_id, docket_data["docket_number"]
        )
    rds_created, content_updated = merge_pacer_docket_into_cl_docket(
        d, pacer_case_id, docket_data, report, appellate=False
    )
    return {
        "docket_pk": d.pk,
        "content_updated": bool(rds_created or content_updated),
    }


@app.task(
    bind=True,
    max_retries=3,
    interval_start=5,
    interval_step=5,
    ignore_result=True,
)
def fetch_docket(self, fq_pk):
    """Fetch a docket from PACER

    This mirrors code elsewhere that gets dockets, but manages status as it
    goes through the process.

    :param fq_pk: The PK of the RECAP Fetch Queue to update.
    :return: None
    """
    fq = PacerFetchQueue.objects.get(pk=fq_pk)
    mark_pq_status(fq, "", PROCESSING_STATUS.IN_PROGRESS)

    cookies = get_pacer_cookie_from_cache(fq.user_id)
    if cookies is None:
        msg = f"Cookie cache expired before task could run for user: {fq.user_id}"
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)

    court_id = fq.court_id or getattr(fq.docket, "court_id", None)
    s = PacerSession(cookies=cookies)

    try:
        result = fetch_pacer_case_id_and_title(s, fq, court_id)
    except (requests.RequestException, ReadTimeoutError) as exc:
        msg = "Network error getting pacer_case_id for fq: %s."
        if self.request.retries == self.max_retries:
            mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
            self.request.chain = None
            return None
        mark_fq_status(
            fq, f"{msg}Retrying.", PROCESSING_STATUS.QUEUED_FOR_RETRY
        )
        raise self.retry(exc=exc)
    except PacerLoginException as exc:
        msg = "PacerLoginException while getting pacer_case_id for fq: %s."
        if self.request.retries == self.max_retries:
            mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
            self.request.chain = None
            return None
        mark_fq_status(
            fq, f"{msg}Retrying.", PROCESSING_STATUS.QUEUED_FOR_RETRY
        )
        raise self.retry(exc=exc)
    except ParsingException:
        msg = "Unable to parse pacer_case_id for docket."
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        self.request.chain = None
        return None

    # result can be one of three values:
    #   None       --> Sealed or missing case
    #   Empty dict --> Didn't run the pacer_case_id lookup (wasn't needed)
    #   Full dict  --> Ran the query, got back results
    if result is None:
        msg = "Cannot find case by docket number (perhaps it's sealed?)"
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        self.request.chain = None
        return None

    pacer_case_id = getattr(fq.docket, "pacer_case_id", None) or result.get(
        "pacer_case_id"
    )

    if not pacer_case_id:
        msg = "Unable to determine pacer_case_id for docket."
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        self.request.chain = None
        return None

    try:
        result = fetch_docket_by_pacer_case_id(s, court_id, pacer_case_id, fq)
    except (requests.RequestException, ReadTimeoutError) as exc:
        msg = "Network error getting pacer_case_id for fq: %s."
        if self.request.retries == self.max_retries:
            mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
            self.request.chain = None
            return None
        mark_fq_status(
            fq, f"{msg}Retrying.", PROCESSING_STATUS.QUEUED_FOR_RETRY
        )
        raise self.retry(exc=exc)
    except ParsingException:
        msg = "Unable to parse pacer_case_id for docket."
        mark_fq_status(fq, msg, PROCESSING_STATUS.FAILED)
        self.request.chain = None
        return None

    msg = "Successfully got and merged docket. Adding to Solr as final step."
    mark_fq_status(fq, msg, PROCESSING_STATUS.SUCCESSFUL)
    return result


@app.task
def mark_fq_successful(fq_pk):
    fq = PacerFetchQueue.objects.get(pk=fq_pk)
    msg = "Successfully completed fetch and save."
    mark_fq_status(fq, msg, PROCESSING_STATUS.SUCCESSFUL)


def mark_fq_status(fq, msg, status):
    """Update the PacerFetchQueue item with the status and message provided

    :param fq: The PacerFetchQueue item to update
    :param msg: The message to associate
    :param status: The status code to associate. If SUCCESSFUL, date_completed
    is set as well.
    :return: None
    """
    fq.message = msg
    fq.status = status
    if status == PROCESSING_STATUS.SUCCESSFUL:
        fq.date_completed = now()
    fq.save()
