import collections
import json
import concurrent.futures
import csv
from os import path
import io
import logging
import os
import shutil
from tempfile import TemporaryDirectory
import warnings
import zlib

from typing import Dict, List

import gzip
import zipfile

from civis import APIClient
from civis.io._utils import maybe_get_random_name
from civis.base import EmptyResultError, CivisImportError, CivisAPIError
from civis.futures import CivisFuture
from civis.io import civis_to_file, file_to_civis
from civis.utils import run_job
from civis._deprecation import DeprecatedKwargDefault

import requests

try:
    import pandas as pd

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    import polars as pl

    HAS_POLARS = True
except ImportError:
    HAS_POLARS = False

CHUNK_SIZE = 32 * 1024
log = logging.getLogger(__name__)

DELIMITERS = {
    ",": "comma",
    "\t": "tab",
    "|": "pipe",
}

_File = collections.namedtuple("_File", "id name detected_info")

_SQL_PARAMS_ARGUMENTS_KEYS = frozenset(("params", "arguments"))

_RETURN_AS_OPTIONS = frozenset(("list", "pandas", "polars"))


def _validate_return_as(return_as):
    if return_as not in _RETURN_AS_OPTIONS:
        raise ValueError(f"unsupported return_as option: {return_as}")

    if return_as == "pandas" and not HAS_PANDAS:
        raise ImportError("return_as is 'pandas' but pandas is not installed.")
    elif return_as == "polars" and not HAS_POLARS:
        raise ImportError("return_as is 'polars' but polars is not installed.")


def _warn_or_raise_for_use_pandas(use_pandas, return_as):
    """When it's time to actually remove use_pandas at civis-python v3.0.0,
    Just remove this helper function as well as all usage of use_pandas."""

    if not isinstance(use_pandas, DeprecatedKwargDefault):
        warn_msg = (
            "To support multiple dataframe libraries, the keyword argument "
            "'use_pandas' is deprecated since civis-python v2.6.0 "
            "and will be removed in civis-python v3.0.0 "
            "(no release timeline yet). "
            "While 'use_pandas' still works for now, you're strongly encouraged to "
            "update your code to use the new keyword argument 'return_as' instead and "
            "stop settting 'use_pandas'. "
        )
        conflict_msg = (
            "Update your code so that the 'use_pandas' argument is no longer set, "
            "and set 'return_as' to one of {'list', 'pandas', 'polars'}."
        )
        if use_pandas and return_as == "list":
            # return_as has a default value of "list". If return_as is "list",
            # there's no way to tell if the user sets it explicitly or if it's
            # from the default value (unless we either rewrite the function signature
            # or use a decorator, which we can't or don't want to).
            # So when use_pandas=True and return_as="list", assume the user's intent
            # is to return a pandas dataframe.
            return_as = "pandas"
            warn_msg += 'To return a pandas dataframe, set return_as="pandas". '
        elif use_pandas and return_as == "polars":
            raise ValueError(
                "Conflicting argument values: use_pandas=True but return_as='polars'. "
                + conflict_msg
            )
        elif not use_pandas and return_as == "pandas":
            raise ValueError(
                "Conflicting argument values: use_pandas=False but return_as='pandas'. "
                + conflict_msg
            )
        # stacklevel=3 to point the warning to the user's code
        warnings.warn(warn_msg.strip(), FutureWarning, stacklevel=3)
    return return_as


def read_civis(
    table,
    database,
    columns=None,
    return_as="list",
    use_pandas=DeprecatedKwargDefault(),
    encoding=None,
    job_name=None,
    client=None,
    credential_id=None,
    polling_interval=None,
    hidden=True,
    **kwargs,
):
    """Read data from a Civis table.

    Parameters
    ----------
    table : str
        Name of table, including schema, in the database. E.g.
        ``'my_schema.my_table'``. Schemas or tablenames with periods must
        be double quoted, e.g. ``'my_schema."my.table"'``.
    database : str or int
        Read data from this database. Can be the database name or ID.
    columns : list, optional
        A list of column names. Column SQL transformations are possible.
        If omitted, all columns are exported.
    return_as : str, {"list", "pandas", "polars"}
        If ``"list"`` (the default), return a list.
        If ``"pandas"``, return a :class:`pandas.DataFrame`.
        If ``"polars"``, return a :class:`polars.DataFrame`.
    use_pandas : bool, optional
        If ``True``, return a :class:`pandas:pandas.DataFrame`. Otherwise,
        return a list of results from :func:`python:csv.reader`.

        .. deprecated:: 2.6.0
            ``use_pandas`` will be removed at civis-python v3.0.0.
            Please use ``return_as`` instead.
    encoding : str, optional
        If ``return_as`` is ``"pandas"`` or ``"polars"``, this parameter is passed to
        the ``encoding`` kwarg of :func:`pandas.read_csv` or
        :func:`polars.read_csv`, respectively.
        If ``return_as`` is ``"list"``, and if this parameter isn't provided,
        then the UTF-8 encoding is assumed. In case you encounter
        a ``UnicodeDecodeError``, consider choosing an encoding suitable
        for your data; see the `list of standard encodings
        <https://docs.python.org/3/library/codecs.html#standard-encodings>`_.
    job_name : str, optional
        A name to give the job. If omitted, a random job name will be
        used.
    client : :class:`civis.APIClient`, optional
        If not provided, an :class:`civis.APIClient` object will be
        created from the :envvar:`CIVIS_API_KEY`.
    credential_id : str or int, optional
        The database credential ID.  If ``None``, the default credential
        will be used.
    polling_interval : int or float, optional
        Number of seconds to wait between checks for query completion.
    hidden : bool, optional
        If ``True`` (the default), this job will not appear in the Civis UI.
    **kwargs : kwargs
        Extra keyword arguments are passed into
        :func:`pandas.read_csv` if ``return_as`` is ``"pandas"``, or
        passed into :func:`python:csv.reader` if ``return_as`` is
        ``"list"``,
        or passed into :func:`polars.read_csv` if ``return_as`` is ``"polars"``.

    Returns
    -------
    data : list | :class:`pandas.DataFrame` | :class:`polars.DataFrame`
        A list of rows (with header as first row) if ``return_as`` is
        ``"list"``, or :class:`pandas.DataFrame` if ``return_as`` is ``"pandas"``,
        or :class:`polars.DataFrame` if ``return_as`` is ``"polars"``.
        Note that if ``return_as`` is ``"list"``, no parsing of types is performed and
        each row will be a list of strings.

    Raises
    ------
    ImportError
        If ``return_as`` is ``"pandas"`` and pandas is not installed.
        If ``return_as`` is ``"polars"`` and polars is not installed.
    EmptyResultError
        If the table is empty.

    Examples
    --------
    >>> import civis
    >>> table = "schema.table"
    >>> database = "my_data"
    >>> columns = ["column_a", "ROW_NUMBER() OVER(ORDER BY date) AS order"]
    >>> data = civis.io.read_civis(table, database, columns=columns)
    >>> columns = data.pop(0)
    >>> col_a_index = columns.index("column_a")
    >>> col_a = [row[col_a_index] for row in data]

    >>> df = civis.io.read_civis("schema.table", "my_data", return_as="pandas")
    >>> col_a = df["column_a"]

    See Also
    --------
    civis.io.read_civis_sql : Read directly into memory using SQL.
    civis.io.civis_to_csv : Write directly to csv.
    civis.io.export_to_civis_file : Store a SQL query's results in a Civis file
    """
    _validate_return_as(return_as)
    return_as = _warn_or_raise_for_use_pandas(use_pandas, return_as)

    if client is None:
        client = APIClient()
    sql = _get_sql_select(table, columns)
    data = read_civis_sql(
        sql=sql,
        database=database,
        return_as=return_as,
        encoding=encoding,
        job_name=job_name,
        client=client,
        credential_id=credential_id,
        polling_interval=polling_interval,
        hidden=hidden,
        **kwargs,
    )
    return data


def export_to_civis_file(
    sql,
    database,
    sql_params_arguments=None,
    job_name=None,
    client=None,
    credential_id=None,
    polling_interval=None,
    hidden=True,
    csv_settings=None,
):
    """Store results of a query to a Civis file

    Parameters
    ----------
    sql : str
        The SQL select string to be executed.
    database : str or int
        Execute the query against this database. Can be the database name
        or ID.
    sql_params_arguments : dict, optional
        A dictionary of SQL query parameters to pass directly to
        :func:`civis.APIClient.scripts.post_sql<civis.resources._resources.Scripts.post_sql>`.
        The only allowed keys are ``"params"`` (whose value is a list[dict]) and
        ``"arguments"`` (whose value is a dict). Please refer to the linked API
        documentation for how to format these two keys' values.
    job_name : str, optional
        A name to give the job. If omitted, a random job name will be
        used.
    client : :class:`civis.APIClient`, optional
        If not provided, an :class:`civis.APIClient` object will be
        created from the :envvar:`CIVIS_API_KEY`.
    credential_id : str or int, optional
        The database credential ID.  If ``None``, the default credential
        will be used.
    polling_interval : int or float, optional
        Number of seconds to wait between checks for query completion.
    hidden : bool, optional
        If ``True`` (the default), this job will not appear in the Civis UI.
    csv_settings : dict, optional
        A dictionary of csv_settings to pass to
        :func:`civis.APIClient.scripts.post_sql`.

    Returns
    -------
    fut : :class:`~civis.futures.CivisFuture`
        A future which returns the response from
        :func:`civis.APIClient.scripts.get_sql_runs` after the sql query
        has completed and the result has been stored as a Civis file.

    Examples
    --------
    >>> import civis
    >>> sql = "SELECT * FROM schema.table"
    >>> fut = civis.io.export_to_civis_file(sql, "my_database")
    >>> file_id = fut.result()['output'][0]["file_id"]


    See Also
    --------
    civis.io.read_civis : Read directly into memory without SQL.
    civis.io.read_civis_sql : Read results of a SQL query into memory.
    civis.io.civis_to_csv : Write directly to a CSV file.
    civis.io.civis_file_to_table : Upload a Civis file to a Civis table
    """
    client = client or APIClient()
    script_id, run_id = _sql_script(
        client=client,
        sql=sql,
        database=database,
        job_name=job_name,
        credential_id=credential_id,
        csv_settings=csv_settings,
        hidden=hidden,
        sql_params_arguments=sql_params_arguments,
    )
    fut = CivisFuture(
        client.scripts.get_sql_runs,
        (script_id, run_id),
        polling_interval=polling_interval,
        client=client,
        poll_on_creation=False,
    )
    return fut


def read_civis_sql(
    sql,
    database,
    return_as="list",
    use_pandas=DeprecatedKwargDefault(),
    sql_params_arguments=None,
    encoding=None,
    job_name=None,
    client=None,
    credential_id=None,
    polling_interval=None,
    hidden=True,
    **kwargs,
):
    """Read data from Civis using a custom SQL string.

    If no data is expected to return from the query,
    consider :func:`~civis.io.query_civis` instead.

    Parameters
    ----------
    sql : str
        The SQL select string to be executed.
    database : str or int
        Execute the query against this database. Can be the database name
        or ID.
    return_as : str, {"list", "pandas", "polars"}
        If ``"list"`` (the default), return a list.
        If ``"pandas"``, return a :class:`pandas.DataFrame`.
        If ``"polars"``, return a :class:`polars.DataFrame`.
    use_pandas : bool, optional
        If ``True``, return a :class:`pandas:pandas.DataFrame`. Otherwise,
        return a list of results from :func:`python:csv.reader`.

        .. deprecated:: 2.6.0
            ``use_pandas`` will be removed at civis-python v3.0.0.
            Please use ``return_as`` instead.
    sql_params_arguments : dict, optional
        A dictionary of SQL query parameters to pass directly to
        :func:`civis.APIClient.scripts.post_sql<civis.resources._resources.Scripts.post_sql>`.
        The only allowed keys are ``"params"`` (whose value is a list[dict]) and
        ``"arguments"`` (whose value is a dict). Please refer to the linked API
        documentation for how to format these two keys' values.
    encoding : str, optional
        If ``return_as`` is ``"pandas"`` or ``"polars"``, this parameter is passed to
        the ``encoding`` kwarg of :func:`pandas.read_csv` or
        :func:`polars.read_csv`, respectively.
        If ``return_as`` is ``"list"``, and if this parameter isn't provided,
        then the UTF-8 encoding is assumed. In case you encounter
        a ``UnicodeDecodeError``, consider choosing an encoding suitable
        for your data; see the `list of standard encodings
        <https://docs.python.org/3/library/codecs.html#standard-encodings>`_.
    job_name : str, optional
        A name to give the job. If omitted, a random job name will be
        used.
    client : :class:`civis.APIClient`, optional
        If not provided, an :class:`civis.APIClient` object will be
        created from the :envvar:`CIVIS_API_KEY`.
    credential_id : str or int, optional
        The database credential ID.  If ``None``, the default credential
        will be used.
    polling_interval : int or float, optional
        Number of seconds to wait between checks for query completion.
    hidden : bool, optional
        If ``True`` (the default), this job will not appear in the Civis UI.
    **kwargs : kwargs
        Extra keyword arguments are passed into
        :func:`pandas.read_csv` if ``return_as`` is ``"pandas"``, or
        passed into :func:`python:csv.reader` if ``return_as`` is ``"list"``,
        or passed into :func:`polars.read_csv` if ``return_as`` is ``"polars"``.

    Returns
    -------
    data : list | :class:`pandas.DataFrame` | :class:`polars.DataFrame`
        A list of rows (with header as first row) if ``return_as`` is
        ``"list"``, or :class:`pandas.DataFrame` if ``return_as`` is ``"pandas"``,
        or :class:`polars.DataFrame` if ``return_as`` is ``"polars"``.
        Note that if ``return_as`` is ``"list"``, no parsing of types is performed and
        each row will be a list of strings.

    Raises
    ------
    ImportError
        If ``return_as`` is ``"pandas"`` and pandas is not installed.
        If ``return_as`` is ``"polars"`` and polars is not installed.
    EmptyResultError
        If no rows were returned as a result of the query.

    Examples
    --------
    >>> import civis
    >>> sql = "SELECT * FROM schema.table"
    >>> df = civis.io.read_civis_sql(sql, "my_database", return_as="pandas")
    >>> col_a = df["column_a"]

    >>> data = civis.io.read_civis_sql(sql, "my_database")
    >>> columns = data.pop(0)
    >>> col_a_index = columns.index("column_a")
    >>> col_a = [row[col_a_index] for row in data]

    Notes
    -----
    This reads the data into memory.

    See Also
    --------
    civis.io.read_civis : Read directly into memory without SQL.
    civis.io.civis_to_csv : Write directly to a CSV file.
    """
    _validate_return_as(return_as)
    return_as = _warn_or_raise_for_use_pandas(use_pandas, return_as)

    if client is None:
        client = APIClient()

    db_id = client.get_database_id(database)
    credential_id = credential_id or client.default_database_credential_id

    script_id, run_id = _sql_script(
        client,
        sql,
        db_id,
        job_name,
        credential_id,
        csv_settings={"compression": "gzip"},
        hidden=hidden,
        sql_params_arguments=sql_params_arguments,
    )
    fut = CivisFuture(
        client.scripts.get_sql_runs,
        (script_id, run_id),
        polling_interval=polling_interval,
        client=client,
        poll_on_creation=False,
    )
    fut.result()
    outputs = client.scripts.get_sql_runs(script_id, run_id)["output"]
    if not outputs:
        raise EmptyResultError("Query {} returned no output.".format(script_id))

    url = outputs[0]["path"]
    file_id = outputs[0]["file_id"]
    log.debug(
        "Exported results to Civis file %s (%s)", outputs[0]["output_name"], file_id
    )

    if return_as == "pandas":
        kwargs["compression"] = "gzip"
        kwargs["encoding"] = encoding

        data = pd.read_csv(url, **kwargs)
    elif return_as == "polars":
        kwargs["encoding"] = encoding
        data = pl.read_csv(url, **kwargs)
    else:
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()

        with io.StringIO() as buf:
            _decompress_stream(
                response, buf, write_bytes=False, encoding=encoding or "utf-8"
            )
            buf.seek(0)
            data = list(csv.reader(buf, **kwargs))

    return data


def civis_to_csv(
    filename,
    sql,
    database,
    sql_params_arguments=None,
    job_name=None,
    client=None,
    credential_id=None,
    include_header=True,
    compression="none",
    delimiter=",",
    unquoted=False,
    hidden=True,
    polling_interval=None,
):
    """Export data from Civis to a local CSV file.

    Parameters
    ----------
    filename : str
        Download exported data into this file.
    sql : str
        The SQL select string to be executed.
    database : str or int
        Export data from this database. Can be the database name or ID.
    sql_params_arguments : dict, optional
        A dictionary of SQL query parameters to pass directly to
        :func:`civis.APIClient.scripts.post_sql<civis.resources._resources.Scripts.post_sql>`.
        The only allowed keys are ``"params"`` (whose value is a list[dict]) and
        ``"arguments"`` (whose value is a dict). Please refer to the linked API
        documentation for how to format these two keys' values.
    job_name : str, optional
        A name to give the job. If omitted, a random job name will be
        used.
    client : :class:`civis.APIClient`, optional
        If not provided, an :class:`civis.APIClient` object will be
        created from the :envvar:`CIVIS_API_KEY`.
    credential_id : str or int, optional
        The ID of the database credential.  If ``None``, the default
        credential will be used.
    include_header: bool, optional
        If ``True``, the first line of the CSV will be headers.
        Default: ``True``.
    compression: str, optional
        Type of compression to use, if any. One of ``'none'``, ``'zip'``, or
        ``'gzip'``. Default ``'none'``. ``'gzip'`` currently returns a file
        with no compression unless include_header is set to False. In a
        future release, a ``'gzip'`` compressed file will be returned for
        all cases.
    delimiter: str, optional
        Which delimiter to use, if any. One of ``','``, ``'\t'``, or
        ``'|'``. Default: ``','``.
    unquoted: bool, optional
        Whether or not to quote fields. Default: ``False``.
    polling_interval : int or float, optional
        Number of seconds to wait between checks for query completion.
    hidden : bool, optional
        If ``True`` (the default), this job will not appear in the Civis UI.

    Returns
    -------
    results : :class:`~civis.futures.CivisFuture`
        A ``CivisFuture`` object that represents the SQL query that unloads data
        from Civis Platform. Note that this unloading process has an arbitrary
        filename assigned and made available through the ``CivisFuture`` object,
        which is not the same as the `filename` parameter. It is recommended
        that a separate variable is used to store the desired filename beforehand
        so that it's accessible after this function finishes,
        see the code example below.

    Examples
    --------
    >>> import civis
    >>> sql = "SELECT * FROM schema.table"
    >>> file_path = "file.csv"
    >>> fut = civis.io.civis_to_csv(file_path, sql, "my_database")
    >>> fut.result()  # Wait for job to complete
    >>> import pandas as pd  # Let's say we want to read the data into a DataFrame
    >>> df = pd.read_csv(file_path)

    See Also
    --------
    civis.io.read_civis : Read table contents into memory.
    civis.io.read_civis_sql : Read results of a SQL query into memory.
    civis.io.export_to_civis_file : Store a SQL query's results in a Civis file
    """
    if client is None:
        client = APIClient()

    db_id = client.get_database_id(database)
    credential_id = credential_id or client.default_database_credential_id

    # don't fix bug that would cause breaking change for now
    # when gzip compression is requested, a gzip file is not actually returned
    # instead the gzip file is decompressed during download
    if compression == "gzip" and include_header:
        compression = "none"

    # don't support parallel unload; the output format
    # is different which would introduce a breaking change
    headers = b""

    delimiter = DELIMITERS.get(delimiter)
    if not delimiter:
        raise ValueError("delimiter must be one of {}".format(DELIMITERS.keys()))

    # always set compression to gzip to reduce I/O
    csv_settings = dict(
        include_header=include_header,
        compression="gzip",
        column_delimiter=delimiter,
        unquoted=unquoted,
        filename_prefix=None,
        force_multifile=False,
    )

    script_id, run_id = _sql_script(
        client,
        sql,
        db_id,
        job_name,
        credential_id,
        hidden=hidden,
        csv_settings=csv_settings,
        sql_params_arguments=sql_params_arguments,
    )
    fut = CivisFuture(
        client.scripts.get_sql_runs,
        (script_id, run_id),
        polling_interval=polling_interval,
        client=client,
        poll_on_creation=False,
    )
    download = _download_callback(script_id, run_id, filename, headers, compression)
    fut.add_done_callback(download)

    return fut


def civis_to_multifile_csv(
    sql,
    database,
    sql_params_arguments=None,
    job_name=None,
    client=None,
    credential_id=None,
    include_header=True,
    compression="none",
    delimiter="|",
    max_file_size=None,
    unquoted=False,
    prefix=None,
    polling_interval=None,
    hidden=True,
):
    """Unload the result of SQL query and return presigned urls.

    This function is intended for unloading large queries/tables from redshift
    as it uses a 'PARALLEL ON' S3 unload. It returns a similar manifest file
    to conventional S3 UNLOAD statements except the CSV parts are accessible
    via both files endpoint IDs and presigned S3 urls.

    Parameters
    ----------
    sql : str
        The SQL select string to be executed.
    database : str or int
        Execute the query against this database. Can be the database name
        or ID.
    sql_params_arguments : dict, optional
        A dictionary of SQL query parameters to pass directly to
        :func:`civis.APIClient.scripts.post_sql<civis.resources._resources.Scripts.post_sql>`.
        The only allowed keys are ``"params"`` (whose value is a list[dict]) and
        ``"arguments"`` (whose value is a dict). Please refer to the linked API
        documentation for how to format these two keys' values.
    job_name : str, optional
        A name to give the job. If omitted, a random job name will be
        used.
    client : :class:`civis.APIClient`, optional
        If not provided, an :class:`civis.APIClient` object will be
        created from the :envvar:`CIVIS_API_KEY`.
    credential_id : str or int, optional
        The database credential ID.  If ``None``, the default credential
        will be used.
    include_header: bool, optional
        If ``True`` include a key in the returned dictionary containing a list
        of column names. Default: ``True``.
    compression: str, optional
        Type of compression to use, if any. One of ``'none'``, ``'zip'``, or
        ``'gzip'``. Default ``'none'``.
    delimiter: str, optional
        Which delimiter to use, if any. One of ``','``, ``'\t'``, or
        ``'|'``. Default: ``'|'``.
    max_file_size: int, optional
        Maximum number of Megabytes each created file will be.
    unquoted: bool, optional
        Whether or not to quote fields. Default: ``False``.
    prefix: str, optional
        A user specified filename prefix for the output file to have. Default:
        ``None``.
    polling_interval : int or float, optional
        Number of seconds to wait between checks for query completion.
    hidden : bool, optional
        If ``True`` (the default), this job will not appear in the Civis UI.

    Returns
    -------
    unload_manifest: dict
        A dictionary resembling an AWS manifest file. Has the following keys:

        'query': str
            The query.

        'header': list of str
            The columns from the query.

        'entries': list of dict
            Each dict has the following keys:

            'id': int
                File ID
            'name': str
                Filename
            'size': int
                File size in bytes
            'url': str
                Unsigned S3 URL ('s3://...')
            'url_signed': str
                Signed S3 URL ('https://...')

        'unquoted': bool
            Whether the cells are quoted.

        'compression': str
            Type of compression used.

        'delimiter': str
            Delimiter that separates the cells.

    Examples
    --------
    >>> import io
    >>> import civis
    >>> import pandas as pd
    >>> sql = "SELECT * FROM schema.my_big_table"
    >>> database = "my_database"
    >>> delimiter = "|"
    >>> manifest = civis.io.civis_to_multifile_csv(sql, database, delimiter=delimiter)
    >>> ids = [entry['id'] for entry in manifest['entries']]
    >>> for file_id in ids:
    >>>     buf = io.BytesIO()
    >>>     civis.io.civis_to_file(file_id, buf)
    >>>     buf.seek(0)
    >>>     # Process the data in `buf` for your own application, e.g.:
    >>>     df = pd.read_csv(buf, delimiter=delimiter)

    See Also
    --------
    civis.APIClient.scripts.post_sql
    """
    if client is None:
        client = APIClient()
    delimiter = DELIMITERS.get(delimiter)
    if not delimiter:
        raise ValueError(f"delimiter must be one of {DELIMITERS.keys()}: {delimiter}")

    csv_settings = dict(
        include_header=include_header,
        compression=compression,
        column_delimiter=delimiter,
        unquoted=unquoted,
        filename_prefix=prefix,
        force_multifile=True,
        max_file_size=max_file_size,
    )
    script_id, run_id = _sql_script(
        client,
        sql,
        database,
        job_name,
        credential_id,
        hidden,
        csv_settings=csv_settings,
        sql_params_arguments=sql_params_arguments,
    )

    fut = CivisFuture(
        client.scripts.get_sql_runs,
        (script_id, run_id),
        polling_interval=polling_interval,
        client=client,
        poll_on_creation=False,
    )

    outputs = fut.result()["output"]
    if not outputs:
        raise EmptyResultError(
            "Unload query {} returned no manifest.".format(script_id)
        )

    buf = io.BytesIO()
    civis_to_file(outputs[0]["file_id"], buf, client=client)
    txt = io.TextIOWrapper(buf, encoding="utf-8")
    txt.seek(0)
    unload_manifest = json.load(txt)

    return unload_manifest


def dataframe_to_civis(
    df,
    database,
    table,
    client=None,
    max_errors=None,
    existing_table_rows="fail",
    diststyle=None,
    distkey=None,
    sortkey1=None,
    sortkey2=None,
    table_columns=None,
    credential_id=None,
    primary_keys=None,
    last_modified_keys=None,
    execution="immediate",
    polling_interval=None,
    hidden=True,
    **kwargs,
):
    """Upload a dataframe into a Civis table.

    For a pandas dataframe,
    the dataframe's index will not be included. To store the index
    along with the other values, use ``df.reset_index()`` instead
    of ``df`` as the first argument to this function.

    Parameters
    ----------
    df : :class:`pandas.DataFrame` | :class:`polars.DataFrame`
        The dataframe to upload to Civis.
    database : str or int
        Upload data into this database. Can be the database name or ID.
    table : str
        The schema and table you want to upload to. E.g.,
        ``'scratch.table'``. Schemas or tablenames with periods must
        be double quoted, e.g. ``'scratch."my.table"'``.
    client : :class:`civis.APIClient`, optional
        If not provided, an :class:`civis.APIClient` object will be
        created from the :envvar:`CIVIS_API_KEY`.
    max_errors : int, optional
        The maximum number of rows with errors to remove from the import
        before failing.
    existing_table_rows : str, optional
        The behaviour if a table with the requested name already exists.
        One of ``'fail'``, ``'truncate'``, ``'append'``, ``'drop'``, or
        ``'upsert'``. Defaults to ``'fail'``.
    diststyle : str, optional
        The distribution style for the table.
        One of ``'even'``, ``'all'`` or ``'key'``.
    distkey : str, optional
        The column to use as the distkey for the table.
    sortkey1 : str, optional
        The column to use as the sortkey for the table.
    sortkey2 : str, optional
        The second column in a compound sortkey for the table.
    table_columns : list[Dict[str, str]], optional
        A list of dictionaries, ordered so each dictionary corresponds
        to a column in the order that it appears in the source file. Each dict
        should have a key "name" that corresponds to the column name in the
        destination table, and a key "sql_type" corresponding to the intended
        column data type in the destination table. The "sql_type" key is not
        required when appending to an existing table. The table_columns
        parameter is required if the table does not exist, the table is being
        dropped, or the columns in the source file do not appear in the same
        order as in the destination table. Example:
        ``[{"name": "foo", "sql_type": "INT"}, {"name": "bar", "sql_type": "VARCHAR"}]``
    credential_id : str or int, optional
        The ID of the database credential.  If ``None``, the default
        credential will be used.
    primary_keys: list[str], optional
        A list of the primary key column(s) of the destination table that
        uniquely identify a record. These columns must not contain null values.
        If existing_table_rows is "upsert", this
        field is required. Note that this is true regardless of whether the
        destination database itself requires a primary key.
    last_modified_keys: list[str], optional
        A list of the columns indicating a record has been updated. If
        existing_table_rows is "upsert", this field is required.
    execution: string, optional, default "immediate"
        One of "delayed" or "immediate". If "immediate", refresh column
        statistics as part of the run. If "delayed", flag the table for a
        deferred statistics update; column statistics may not be available
        for up to 24 hours. In addition, if existing_table_rows is "upsert",
        delayed executions move data from staging table to final table after a
        brief delay, in order to accommodate multiple concurrent imports to the
        same destination table.
    polling_interval : int or float, optional
        Number of seconds to wait between checks for job completion.
    hidden : bool, optional
        If ``True`` (the default), this job will not appear in the Civis UI.
    **kwargs : kwargs
        Extra keyword arguments will be passed to
        :func:`pandas.DataFrame.to_csv`
        or :func:`polars.DataFrame.write_csv`.

    Returns
    -------
    fut : :class:`~civis.futures.CivisFuture`
        A ``CivisFuture`` object.

    Examples
    --------
    >>> import civis
    >>> import pandas as pd
    >>> df = pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]})
    >>> fut = civis.io.dataframe_to_civis(df, 'my-database',
    ...                                   'scratch.df_table')
    >>> fut.result()

    See Also
    --------
    :func:`pandas.DataFrame.to_csv`
    :func:`polars.DataFrame.write_csv`
    """  # noqa: E501
    if client is None:
        client = APIClient()

    headers = False if kwargs.get("header") is False else True
    with TemporaryDirectory() as tmp_dir:
        tmp_path = os.path.join(tmp_dir, "dataframe_to_civis.csv")
        if (df_lib := df.__module__.split(".")[0]) == "pandas":
            to_csv_kwargs = {"encoding": "utf-8", "index": False}
            to_csv_kwargs.update(kwargs)
            df.to_csv(tmp_path, **to_csv_kwargs)
        elif df_lib == "polars":
            df.write_csv(tmp_path, **kwargs)
        else:
            raise ValueError(
                f"unsuppported dataframe library {df_lib!r} "
                "-- only pandas and polars are supported"
            )
        _, name = split_schema_tablename(table)
        file_id = file_to_civis(tmp_path, name, client=client)

    delimiter = ","
    fut = civis_file_to_table(
        file_id,
        database,
        table,
        client=client,
        max_errors=max_errors,
        existing_table_rows=existing_table_rows,
        diststyle=diststyle,
        distkey=distkey,
        sortkey1=sortkey1,
        sortkey2=sortkey2,
        table_columns=table_columns,
        delimiter=delimiter,
        headers=headers,
        credential_id=credential_id,
        primary_keys=primary_keys,
        last_modified_keys=last_modified_keys,
        escaped=False,
        execution=execution,
        polling_interval=polling_interval,
        hidden=hidden,
    )

    return fut


def csv_to_civis(
    filename,
    database,
    table,
    client=None,
    max_errors=None,
    existing_table_rows="fail",
    diststyle=None,
    distkey=None,
    sortkey1=None,
    sortkey2=None,
    table_columns=None,
    delimiter=",",
    headers=None,
    primary_keys=None,
    last_modified_keys=None,
    escaped=False,
    execution="immediate",
    credential_id=None,
    polling_interval=None,
    hidden=True,
):
    """Upload the contents of a local CSV file to Civis.

    Parameters
    ----------
    filename : str
        Upload the contents of this file.
    database : str or int
        Upload data into this database. Can be the database name or ID.
    table : str
        The schema and table you want to upload to. E.g.,
        ``'scratch.table'``.
    client : :class:`civis.APIClient`, optional
        If not provided, an :class:`civis.APIClient` object will be
        created from the :envvar:`CIVIS_API_KEY`.
    max_errors : int, optional
        The maximum number of rows with errors to remove from the import
        before failing.
    existing_table_rows : str, optional
        The behaviour if a table with the requested name already exists.
        One of ``'fail'``, ``'truncate'``, ``'append'``, ``'drop'``, or
        ``'upsert'``. Defaults to ``'fail'``.
    diststyle : str, optional
        The distribution style for the table.
        One of ``'even'``, ``'all'`` or ``'key'``.
    distkey : str, optional
        The column to use as the distkey for the table.
    sortkey1 : str, optional
        The column to use as the sortkey for the table.
    sortkey2 : str, optional
        The second column in a compound sortkey for the table.
    table_columns : list[Dict[str, str]], optional
        A list of dictionaries, ordered so each dictionary corresponds
        to a column in the order that it appears in the source file. Each dict
        should have a key "name" that corresponds to the column name in the
        destination table, and a key "sql_type" corresponding to the intended
        column data type in the destination table. The "sql_type" key is not
        required when appending to an existing table. The table_columns
        parameter is required if the table does not exist, the table is being
        dropped, or the columns in the source file do not appear in the same
        order as in the destination table. Example:
        ``[{"name": "foo", "sql_type": "INT"}, {"name": "bar", "sql_type": "VARCHAR"}]``
    delimiter : string, optional
        The column delimiter. One of ``','``, ``'\\t'`` or ``'|'``.
    headers : bool, optional
        Whether or not the first row of the file should be treated as
        headers. The default, ``None``, attempts to autodetect whether
        or not the first row contains headers.
    primary_keys: list[str], optional
        A list of the primary key column(s) of the destination table that
        uniquely identify a record. These columns must not contain null values.
        If existing_table_rows is "upsert", this
        field is required. Note that this is true regardless of whether the
        destination database itself requires a primary key.
    last_modified_keys: list[str], optional
        A list of the columns indicating a record has been updated. If
        existing_table_rows is "upsert", this field is required.
    escaped: bool, optional
        A boolean value indicating whether or not the source file has quotes
        escaped with a backslash. Defaults to false.
    execution: string, optional, default "immediate"
        One of "delayed" or "immediate". If "immediate", refresh column
        statistics as part of the run. If "delayed", flag the table for a
        deferred statistics update; column statistics may not be available
        for up to 24 hours. In addition, if existing_table_rows is "upsert",
        delayed executions move data from staging table to final table after a
        brief delay, in order to accommodate multiple concurrent imports to the
        same destination table.
    credential_id : str or int, optional
        The ID of the database credential.  If ``None``, the default
        credential will be used.
    polling_interval : int or float, optional
        Number of seconds to wait between checks for job completion.
    hidden : bool, optional
        If ``True`` (the default), this job will not appear in the Civis UI.

    Returns
    -------
    results : :class:`~civis.futures.CivisFuture`
        A `CivisFuture` object.

    Notes
    -----
    This reads the contents of `filename` into memory.

    Examples
    --------
    >>> import civis
    >>> with open('input_file.csv', 'w') as _input:
    ...     _input.write('a,b,c\\n1,2,3')
    >>> fut = civis.io.csv_to_civis('input_file.csv',
    ...                             'my-database',
    ...                             'scratch.my_data')
    >>> fut.result()
    """  # noqa: E501
    if client is None:
        client = APIClient()

    name = path.basename(filename)
    with open(filename, "rb") as data:
        file_id = file_to_civis(data, name, client=client)
        log.debug("Uploaded file %s to Civis file %s", filename, file_id)
        fut = civis_file_to_table(
            file_id,
            database,
            table,
            client=client,
            max_errors=max_errors,
            existing_table_rows=existing_table_rows,
            diststyle=diststyle,
            distkey=distkey,
            sortkey1=sortkey1,
            sortkey2=sortkey2,
            table_columns=table_columns,
            delimiter=delimiter,
            headers=headers,
            credential_id=credential_id,
            primary_keys=primary_keys,
            last_modified_keys=last_modified_keys,
            escaped=escaped,
            execution=execution,
            polling_interval=polling_interval,
            hidden=hidden,
        )
    return fut


def civis_file_to_table(
    file_id,
    database,
    table,
    client=None,
    max_errors=None,
    existing_table_rows="fail",
    diststyle=None,
    distkey=None,
    sortkey1=None,
    sortkey2=None,
    table_columns=None,
    primary_keys=None,
    last_modified_keys=None,
    escaped=False,
    execution="immediate",
    delimiter=None,
    headers=None,
    credential_id=None,
    polling_interval=None,
    hidden=True,
):
    """Upload the contents of one or more Civis files to a Civis table.
    All provided files will be loaded as an atomic unit in parallel, and
    should share the same columns in the same order, and be in the same
    format.

    .. note::
        The input Civis files must be in a CSV-like delimiter separated format and
        will be accepted in both uncompressed and compressed format
        (.zip, .gz).

    Parameters
    ----------
    file_id : int or list[int]
        Civis file ID or a list of Civis file IDs.
    database : str or int
        Upload data into this database. Can be the database name or ID.
    table : str
        The schema and table you want to upload to. E.g.,
        ``'scratch.table'``.
    client : :class:`civis.APIClient`, optional
        If not provided, an :class:`civis.APIClient` object will be
        created from the :envvar:`CIVIS_API_KEY`.
    max_errors : int, optional
        The maximum number of rows with errors to remove from the import
        before failing. If multiple files are provided, this limit applies
        across all files combined.
    existing_table_rows : str, optional
        The behaviour if a table with the requested name already exists.
        One of ``'fail'``, ``'truncate'``, ``'append'``, ``'drop'``, or
        ``'upsert'``. Defaults to ``'fail'``.
    diststyle : str, optional
        The distribution style for the table.
        One of ``'even'``, ``'all'`` or ``'key'``.
    distkey : str, optional
        The column to use as the distkey for the table.
    sortkey1 : str, optional
        The column to use as the sortkey for the table.
    sortkey2 : str, optional
        The second column in a compound sortkey for the table.
    table_columns : list[Dict[str, str]], optional
        A list of dictionaries, ordered so each dictionary corresponds
        to a column in the order that it appears in the source file. Each dict
        should have a key "name" that corresponds to the column name in the
        destination table, and a key "sql_type" corresponding to the intended
        column data type in the destination table. The "sql_type" key is not
        required when appending to an existing table. The table_columns
        parameter is required if the table does not exist, the table is being
        dropped, or the columns in the source file do not appear in the same
        order as in the destination table. Example:
        ``[{"name": "foo", "sql_type": "INT"}, {"name": "bar", "sql_type": "VARCHAR"}]``
    primary_keys: list[str], optional
        A list of the primary key column(s) of the destination table that
        uniquely identify a record. These columns must not contain null values.
        If existing_table_rows is "upsert", this
        field is required. Note that this is true regardless of whether the
        destination database itself requires a primary key.
    last_modified_keys: list[str], optional
        A list of the columns indicating a record has been updated. If
        existing_table_rows is "upsert", this field is required.
    escaped: bool, optional
        A boolean value indicating whether or not the source file(s) escape
        quotes with a backslash. Defaults to false.
    execution: string, optional, default "immediate"
        One of "delayed" or "immediate". If "immediate", refresh column
        statistics as part of the run. If "delayed", flag the table for a
        deferred statistics update; column statistics may not be available
        for up to 24 hours. In addition, if existing_table_rows is "upsert",
        delayed executions move data from staging table to final table after a
        brief delay, in order to accommodate multiple concurrent imports to the
        same destination table.
    delimiter : string, optional
        The column delimiter. One of ``','``, ``'\\t'`` or ``'|'``. If not
        provided, will attempt to auto-detect.
    headers : bool, optional
        Whether or not the first row of the file should be treated as
        headers. The default, ``None``, attempts to autodetect whether
        or not the first row contains headers.
    credential_id : str or int, optional
        The ID of the database credential.  If ``None``, the default
        credential will be used.
    polling_interval : int or float, optional
        Number of seconds to wait between checks for job completion.
    hidden : bool, optional
        If ``True`` (the default), this job will not appear in the Civis UI.

    Returns
    -------
    results : :class:`~civis.futures.CivisFuture`
        A `CivisFuture` object.

    Raises
    ------
    CivisImportError
        If multiple files are given and determined to be incompatible for
        import. This may be the case if their columns have different types,
        their delimiters are different, headers are present in some but not
        others, or compressions do not match.
    TypeError
        If the type of the file_id parameter is a string. This situation may
        arise when the file ID comes from an environment variable and is not
        cast from string to integer before being passed to civis_file_to_table.

    Examples
    --------
    >>> import civis
    >>> file_id = 100
    >>> fut = civis.io.civis_file_to_table(file_id,
    ...                                    'my-database',
    ...                                    'scratch.my_data')
    >>> fut.result()
    """  # noqa: E501
    if client is None:
        client = APIClient()

    if type(file_id) is str:
        raise TypeError("Invalid type for file_id: str. " "Must be int or list[int]")

    schema, table_name = split_schema_tablename(table)
    if isinstance(file_id, int):
        file_id = [file_id]
    if not file_id:
        raise ValueError("Provide one or multiple meaningful input file IDs.")
    if schema is None:
        raise ValueError("Provide a schema as part of the `table` input.")
    db_id = client.get_database_id(database)
    cred_id = credential_id or client.default_database_credential_id
    if delimiter is not None:  # i.e. it was provided as an argument
        delimiter = DELIMITERS.get(delimiter)
        if not delimiter:
            raise ValueError(
                f"delimiter must be one of {DELIMITERS.keys()}: {delimiter}"
            )
    if table_columns:
        # If the data cleaning code doesn't find a "sql_type" for each
        # entry, it will silently replace the input table_columns with
        # an inferred table_columns. Make sure there's no typos in the input.
        keys = set(key for hash in table_columns for key in hash)
        valid_keys = {"name", "sql_type"}
        invalid_keys = keys - valid_keys
        if invalid_keys:
            # Sort the sets for display to allow for deterministic testing in
            # Python versions < 3.7.
            raise ValueError(
                "Keys of the dictionaries contained in `table_columns` must "
                "be one of {}. The input `table_columns` also has "
                "{}.".format(tuple(sorted(valid_keys)), tuple(sorted(invalid_keys)))
            )

    try:
        client.databases.get_schemas_tables(db_id, schema, table_name)
        log.debug(
            "Table {table} already exists - skipping column "
            "detection".format(table=table)
        )
        table_exists = True
    except CivisAPIError as e:
        table_exists = False
        if e.status_code != 404:
            warnings.warn(
                "Unexpected error when checking if table %s.%s "
                "exists on database %d:\n%s" % (schema, table_name, db_id, str(e))
            )

    sql_types_provided = False
    if table_columns:
        sql_type_cnt = sum(1 for col in table_columns if col.get("sql_type"))
        if sql_type_cnt == len(table_columns):
            sql_types_provided = True
        elif sql_type_cnt != 0:
            error_message = (
                "Some table columns " "have a sql type provided, " "but others do not."
            )
            raise ValueError(error_message)

    # Use Preprocess endpoint to get the table columns as needed
    # and perform necessary file cleaning
    need_table_columns = (not table_exists or existing_table_rows == "drop") and (
        not sql_types_provided
    )

    cleaning_futures = _run_cleaning(
        file_id, client, need_table_columns, headers, delimiter, hidden
    )

    (cleaned_file_ids, headers, compression, delimiter, cleaned_table_columns) = (
        _process_cleaning_results(
            cleaning_futures, client, headers, need_table_columns, delimiter
        )
    )

    table_columns = cleaned_table_columns if need_table_columns else table_columns

    source = dict(file_ids=cleaned_file_ids)
    destination = dict(
        schema=schema,
        table=table_name,
        remote_host_id=db_id,
        credential_id=cred_id,
        primary_keys=primary_keys,
        last_modified_keys=last_modified_keys,
    )

    redshift_options = dict(
        distkey=distkey, sortkeys=[sortkey1, sortkey2], diststyle=diststyle
    )

    import_name = "CSV import to {}.{}".format(schema, table_name)
    import_job = client.imports.post_files_csv(
        source,
        destination,
        headers,
        name=import_name,
        max_errors=max_errors,
        existing_table_rows=existing_table_rows,
        column_delimiter=delimiter,
        compression=compression,
        escaped=escaped,
        execution=execution,
        # If the user hasn't explicitly provided table column info,
        # then there might be differences in their precisions/lengths.
        # Setting this option will allow the Civis API
        # to increase these values for the data types provided,
        # and decreases the risk of a length-related import failure
        # when types are inferred.
        loosen_types=need_table_columns,
        table_columns=table_columns,
        redshift_destination_options=redshift_options,
        hidden=hidden,
    )
    fut = run_job(import_job.id, client=client, polling_interval=polling_interval)
    log.debug("Started run %d for import %d", fut.run_id, import_job.id)
    return fut


def _sql_script(
    client,
    sql,
    database,
    job_name,
    credential_id,
    hidden=False,
    csv_settings=None,
    sql_params_arguments=None,
):
    job_name = maybe_get_random_name(job_name)
    db_id = client.get_database_id(database)
    credential_id = credential_id or client.default_database_credential_id
    csv_settings = csv_settings or {}
    sql_params_arguments = sql_params_arguments or {}

    if not (keys := set(sql_params_arguments.keys())) <= _SQL_PARAMS_ARGUMENTS_KEYS:
        raise ValueError(
            "sql_params_arguments must be a dict with keys in "
            f"{set(_SQL_PARAMS_ARGUMENTS_KEYS)}: {keys}"
        )

    export_job = client.scripts.post_sql(
        job_name,
        remote_host_id=db_id,
        credential_id=credential_id,
        sql=sql,
        hidden=hidden,
        csv_settings=csv_settings,
        **sql_params_arguments,
    )

    run_job = client.scripts.post_sql_runs(export_job.id)
    log.debug("Started run %d of SQL script %d", run_job.id, export_job.id)
    return export_job.id, run_job.id


def _get_sql_select(table, columns=None):
    if columns and not isinstance(columns, (list, tuple)):
        raise TypeError("columns must be a list, tuple or None")
    select = ", ".join(columns) if columns is not None else "*"
    sql = "select {} from {}".format(select, table)  # nosec
    return sql


def _decompress_stream(response, buf, write_bytes=True, encoding="utf-8"):

    # use response.raw for a more consistent approach
    # if content-encoding is specified in the headers
    # then response.iter_content will decompress the stream
    # however, our use of content-encoding is inconsistent
    chunk = response.raw.read(CHUNK_SIZE)
    d = zlib.decompressobj(zlib.MAX_WBITS | 32)

    while chunk or d.unused_data:
        if d.unused_data:
            to_decompress = d.unused_data + chunk
            d = zlib.decompressobj(zlib.MAX_WBITS | 32)
        else:
            to_decompress = d.unconsumed_tail + chunk
        if write_bytes:
            buf.write(d.decompress(to_decompress))
        else:
            buf.write(d.decompress(to_decompress).decode(encoding))
        chunk = response.raw.read(CHUNK_SIZE)


def _download_file(url, local_path, headers, compression):
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()

    # gzipped buffers can be concatenated so write headers as gzip
    if compression == "gzip":
        with gzip.open(local_path, "wb") as fout:
            fout.write(headers)
        with open(local_path, "ab") as fout:
            shutil.copyfileobj(response.raw, fout, CHUNK_SIZE)

    # write headers and decompress the stream
    elif compression == "none":
        with open(local_path, "wb") as fout:
            fout.write(headers)
            _decompress_stream(response, fout)

    # decompress the stream, write headers, and zip the file
    elif compression == "zip":
        with TemporaryDirectory() as tmp_dir:
            tmp_path = path.join(tmp_dir, "civis_to_csv.csv")
            with open(tmp_path, "wb") as tmp_file:
                tmp_file.write(headers)
                _decompress_stream(response, tmp_file)

            with zipfile.ZipFile(local_path, "w") as fout:
                arcname = path.basename(local_path)
                if arcname.split(".")[-1] == "zip":
                    arcname = arcname.split(".")[0] + ".csv"
                fout.write(tmp_path, arcname, zipfile.ZIP_DEFLATED)


def _download_callback(job_id, run_id, filename, headers, compression):

    def callback(future):
        if not future.succeeded():
            return
        outputs = future.result().get("output")
        if not outputs:
            warnings.warn(
                "Job %s, run %s does not have any output to "
                "download. Not creating file %s." % (job_id, run_id, filename),
                RuntimeWarning,
            )
            return
        else:
            url = outputs[0]["path"]
            file_id = outputs[0]["file_id"]
            log.debug("Exported results to Civis file %s", file_id)
            return _download_file(url, filename, headers, compression)

    return callback


def split_schema_tablename(table):
    """Split a Redshift 'schema.tablename' string

    Remember that special characters (such as '.') can only
    be included in a schema or table name if delimited by double-quotes.

    Parameters
    ----------
    table: str
        Either a Redshift schema and table name combined
        with a ".", or else a single table name.

    Returns
    -------
    schema, tablename
        A 2-tuple of strings. The ``schema`` may be None if the input
        is only a table name, but the ``tablename`` will always be filled.

    Raises
    ------
    ValueError
        If the input ``table`` is not separable into a schema and
        table name.
    """
    reader = csv.reader(
        io.StringIO(str(table)), delimiter=".", doublequote=True, quotechar='"'
    )
    schema_name_tup = next(reader)
    if len(schema_name_tup) == 1:
        schema_name_tup = (None, schema_name_tup[0])
    if len(schema_name_tup) != 2:
        raise ValueError(
            "Cannot parse schema and table. "
            "Does '{}' follow the pattern 'schema.table'?".format(table)
        )
    return tuple(schema_name_tup)


def _run_cleaning(
    file_ids,
    client,
    need_table_columns,
    headers,
    delimiter,
    hidden,
    polling_interval=None,
):
    cleaning_futures = []
    for fid in file_ids:
        cleaner_job = client.files.post_preprocess_csv(
            file_id=fid,
            in_place=False,
            detect_table_columns=need_table_columns,
            force_character_set_conversion=True,
            include_header=headers,
            column_delimiter=delimiter,
            hidden=hidden,
        )
        fut = run_job(cleaner_job.id, client=client, polling_interval=polling_interval)
        log.debug(
            "Started CSV preprocess job %d run %d for file %d (%s)",
            cleaner_job.id,
            fut.run_id,
            fid,
            client.files.get(fid).name,
        )
        cleaning_futures.append(fut)
    return cleaning_futures


def _process_cleaning_results(
    cleaning_futures, client, headers, need_table_columns, delimiter
):
    futures, _ = concurrent.futures.wait(cleaning_futures)
    files: List[_File] = []

    job_run_ids_no_output = []
    for fut in futures:
        objs = client.jobs.list_runs_outputs(fut.job_id, fut.run_id)
        if not objs:
            job_run_ids_no_output.append((fut.job_id, fut.run_id))
            continue
        # `objs` is guaranteed to have exactly one file output.
        f = client.files.get(objs[0].object_id)
        files.append(_File(id=f.id, name=f.name, detected_info=f.detected_info))
    if job_run_ids_no_output:
        job_run_ids_in_err_msg = "\n".join(
            f"\tjob {j} run {r}" for j, r in job_run_ids_no_output
        )
        raise CivisImportError(
            "No CSV preprocess output found for "
            f"these runs:\n{job_run_ids_in_err_msg}"
        )

    if need_table_columns:
        table_columns, allow_inconsistent_headers = _check_column_types(files)
    else:
        table_columns = None
        allow_inconsistent_headers = False

    try:
        headers = _check_detected_info(files, "includeHeader", headers)
    except CivisImportError:
        if allow_inconsistent_headers:
            headers = True
        else:
            raise

    delimiter = _check_detected_info(files, "columnDelimiter", delimiter)
    compression = _check_detected_info(files, "compression")

    cleaned_file_ids = [f.id for f in files]

    return cleaned_file_ids, headers, compression, delimiter, table_columns


def _check_detected_info(files: List[_File], attr: str, value_from_user=None):
    values_detected = [f.detected_info[attr] for f in files]
    err_msg = _err_msg_if_inconsistent(values_detected, files)
    if err_msg:
        raise CivisImportError(
            f"All detected values for '{attr}' must be the same, "
            f"however --\n{err_msg}"
        )

    value_detected = values_detected[0]

    # The user cannot specify "compression" from civis_file_to_table.
    if attr == "compression" or value_from_user is None:
        return value_detected

    if value_detected != value_from_user:
        raise CivisImportError(
            f"All detected values for '{attr}' in your files "
            f"are {value_detected}, which doesn't match "
            f"your provided value of {value_from_user}"
        )
    else:
        return value_detected


def _check_column_types(files: List[_File]):
    cols_by_file: List[List[Dict[str, str]]]
    cols_by_file = [f.detected_info["tableColumns"] for f in files]

    col_counts = [len(cols) for cols in cols_by_file]
    err_msg = _err_msg_if_inconsistent(col_counts, files)
    if err_msg:
        raise CivisImportError(
            f"All files must have the same number of columns, " f"however --\n{err_msg}"
        )

    # Transpose cols_by_file to get cols_by_col
    # https://stackoverflow.com/q/6473679
    cols_by_col: List[List[Dict[str, str]]]
    cols_by_col = list(map(list, zip(*cols_by_file)))

    table_columns: List[Dict[str, str]] = []
    allow_inconsistent_headers = False
    err_msgs: List[str] = []

    for i, cols in enumerate(cols_by_col, 1):
        col_name = next((c.get("name") for c in cols if c.get("name")), f"column_{i}")

        sql_base_types = [col["sql_type"].split("(", 1)[0].upper() for col in cols]
        err_msg = _err_msg_if_inconsistent(sql_base_types, files)
        if err_msg and "VARCHAR" not in sql_base_types:
            err_msgs.append(
                f"All sql_types for column '{col_name}' must be the same, "
                f"however --\n{err_msg}"
            )
            continue
        if err_msg:
            # If the sql_types are inconsistent and at least one of them
            # is a VARCHAR, then simply use VARCHAR
            # for this column across all files.
            sql_type = "VARCHAR"
            # The one single file that originally had sql_type as VARCHAR
            # has the detected "first row is headers" to be false
            # (i.e., the detection thought the first row was first row of data,
            # because it can't reliably tell when it comes to VARCHAR),
            # whereas for all the other files where the sql_type for this
            # column isn't VARCHAR, the detected "first row is headers" is
            # true instead. Due to this expected inconsistency,
            # we need to pass a flag out of this function to signal
            # that we can allow `headers` to be True.
            allow_inconsistent_headers = True
        else:
            sql_type = cols[0]["sql_type"]
        table_columns.append({"name": col_name, "sql_type": sql_type})

    if err_msgs:
        raise CivisImportError("\n".join(err_msgs))

    return table_columns, allow_inconsistent_headers


def _err_msg_if_inconsistent(items: List, files: List[_File]):
    if len(set(items)) <= 1:
        return
    values_to_indices = collections.defaultdict(list)
    for i, value in enumerate(items):
        values_to_indices[value].append(i)
    msg_for_each_value = [
        f"\t{v} from: {_format_files_for_err_msg(files, indices)}"
        for v, indices in values_to_indices.items()
    ]
    err_msg = "\n".join(msg_for_each_value)
    return err_msg


def _format_files_for_err_msg(files: List[_File], indices):
    files_in_str = [f"file {files[i].id} ({files[i].name})" for i in indices]
    return ", ".join(files_in_str)
