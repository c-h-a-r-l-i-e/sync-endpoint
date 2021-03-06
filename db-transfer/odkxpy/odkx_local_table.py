import sqlalchemy
from .odkx_server_table import OdkxServerTable, OdkxServerTableRow, OdkxServerTableColumn, OdkxServerColumnDefinition, OdkxServerTableDefinition
from sqlalchemy import MetaData, text
import os
from typing import Optional, List
import hashlib
import requests
import logging
import datetime
import pandas as pd
from enum import Enum

class LocalSyncMode(Enum):
    FULL = 1
    ONLY_NEW_RECORDS = 2
    ONLY_EXISTING_RECORDS = 3

class FilesystemAttachmentStore(object):
    def __init__(self, path, useWindowsPaths: bool = False):
        self.path = path
        self.useWindowsPaths = useWindowsPaths

    def getFileName(self, id, filename):
        xid = id
        if self.useWindowsPaths:
            xid = id.replace(":","")
        return os.path.join(self.path, xid, filename)

    def hasFile(self, id, filename):
        return os.path.isfile(self.getFileName(id, filename))

    def open(self, id, filename):
        return open(self.getFileName(id, filename), 'rb')

    def getMD5(self, id, filename):
        hash_md5 = hashlib.md5()
        with open(self.getFileName(id, filename), "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def storeFile(self, id, filename, response: requests.Response):
        target = self.getFileName(id, filename)
        xid = id
        if self.useWindowsPaths:
            xid = id.replace(":","")
        os.makedirs(os.path.join(self.path, xid),exist_ok=True)
        with open(target + '-tmp', 'wb') as out_file:
            for chunk in response.iter_content(1024):
                out_file.write(chunk)
        if os.path.isfile(target):
            os.remove(target)
        os.rename(target + '-tmp', target)
        del response


class OdkxLocalTable(object):
    def __init__(self, tableId: str, engine: sqlalchemy.engine.Engine, schema: str, attachment_store_path: Optional[str], useWindowsCompatiblePaths: bool, storage):
        self.tableId = tableId
        self._storage = storage
        self.schema = schema
        self.attachments = FilesystemAttachmentStore(os.getcwd() if attachment_store_path is None else attachment_store_path, useWindowsPaths=useWindowsCompatiblePaths)
        self.engine: sqlalchemy.engine.Engine = engine

    def getTableDefinition(self) -> OdkxServerTableDefinition:
        return self._storage.getCachedTableDefinition(self.tableId)

    def getLocalDataETag(self):
        with self.engine.connect() as c:
            rs = c.execute(text(f"""select "dataETag" from {self.schema}.status_table where "table_name" = :tableid order by sync_date desc limit 1
                """), tableid=self.tableId)
            for row in rs:
                return row[0]
        return str('')

    def _getStagingTable(self) -> sqlalchemy.Table:
        meta = MetaData()
        meta.reflect(self.engine, schema=self.schema, only=[self.tableId + '_staging'])
        return meta.tables.get(self.schema+ '.' + self.tableId + '_staging')

    def _getLogTable(self) -> sqlalchemy.Table:
        meta = MetaData()
        meta.reflect(self.engine, schema=self.schema, only=[self.tableId + '_log'])
        return meta.tables.get(self.schema+ '.' + self.tableId + '_log')

    def _getDataTable(self) -> sqlalchemy.Table:
        meta = MetaData()
        meta.reflect(self.engine, schema=self.schema, only=[self.tableId])
        return meta.tables.get(self.schema+ '.' + self.tableId)


    def updateLocalStatusDb(self, dataETag, transaction: sqlalchemy.engine.Connection=None):
        sql = f"""INSERT INTO {self.schema}.status_table ("table_name", "dataETag", "sync_date")
                 VALUES ('{self.tableId}', '{dataETag}', '{str(datetime.datetime.now())}')"""
        def _do(trans):
            trans.execute(sql)
        if transaction is not None:
            _do(transaction)
        else:
            with self.engine.begin() as con:
                con.execute(sql)


    def row_asdict(self, r: OdkxServerTableRow):
        dct = {}
        dct['rowETag'] = r.rowETag
        dct['createUser'] = r.createUser
        dct['lastUpdateUser'] = r.lastUpdateUser
        dct['dataETagAtModification'] = r.dataETagAtModification
        dct['savepointCreator'] = r.savepointCreator
        dct['formId'] = r.formId
        dct['locale'] = r.locale
        dct['savepointType'] = r.savepointType
        dct['savepointTimestamp'] = r.savepointTimestamp
        dct['deleted'] = r.deleted
        dct['defaultAccess'] = r.filterScope.defaultAccess
        dct['groupModify'] = r.filterScope.groupModify
        dct['groupPrivileged'] = r.filterScope.groupPrivileged
        dct['groupReadOnly'] = r.filterScope.groupReadOnly
        dct['rowOwner'] = r.filterScope.rowOwner
        dct['id'] = r.id
        for col in r.orderedColumns:
            assert isinstance(col, OdkxServerTableColumn)
            dct[col.column] =col.value
        return dct


    def stageAllDataChanges(self, remoteTable: OdkxServerTable) -> Optional[str]:
        st = self._getStagingTable()
        last_rs = None
        with self.engine.begin() as transaction:
            transaction.execute(st.delete())
            for rowset in remoteTable.getDiffGenerator(dataETag=self.getLocalDataETag()):
                last_rs = rowset
                if (len(rowset.rows) > 0):
                    transaction.execute(st.insert(), [self.row_asdict(x) for x in rowset.rows])
        if not last_rs is None:
            return last_rs.dataETag

    def downloadAttachments(self, remoteTable: OdkxServerTable, rowId: str, target_file_list: List[str]):
        got_files = []
        for f in remoteTable.getAttachmentsManifest(rowId):
            got_files.append(f.filename)
            if self.attachments.hasFile(rowId, f.filename):
                if self.attachments.getMD5(rowId, f.filename) == f.md5hash:
                    continue
            self.attachments.storeFile(rowId, f.filename,
                                       remoteTable.getAttachment(rowId, f.filename, stream=True, timeout=300))
        missing_files = [x for x in target_file_list if not x in got_files]
        if len(missing_files) > 0:
            print("MISSING FILES (trying again on next sync) for ", rowId, str(missing_files), "\ngot\n" ,str(got_files))
            return False
        return True


    def _sync_pull_attachments(self, remoteTable: OdkxServerTable):
        attach_cols = [x.elementKey for x in remoteTable.getTableDefinition().columns if x.elementType == 'rowpath']
        if len(attach_cols) == 0:
            return
        ids = []
        files_by_id = {}
        with self.engine.connect() as c:
            result = c.execute("select id, {cols} from {schema}.{table} where state='sync_attachments'".format(
                schema=self.schema, table=self.tableId,
                cols=",".join(['"{c}"'.format(c=c) for c in attach_cols])
            ))
            for r in result:
                ids.append(r['id'])
                files_by_id[r['id']] = [r[x] for x in attach_cols if not r[x] is None]
        print("syncing ", len(ids) , " rows attachments")

        for id in ids:
            if self.downloadAttachments(remoteTable, id, files_by_id[id]):
                with self.engine.connect() as c:
                    c.execute(sqlalchemy.sql.text("update {schema}.{table} set state='synced' where id=:rowid".format(
                        schema=self.schema, table=self.tableId)), rowid=id)

    def _staging_to_log(self, transaction: sqlalchemy.engine.Connection = None):
        st = self._getStagingTable()
        colnames = [x.name for x in st.columns]
        fields = ','.join(['"{colname}"'.format(colname=colname) for colname in colnames])
        def _do(trans):
            trans.execute("insert into {schema}.{logtable} ({fields}) select {fields} from {schema}.{stagingtable}".format(
                schema= self.schema,
                logtable=self.tableId+'_log',
                fields=fields,
                stagingtable=self.tableId+'_staging'
            ))
        if transaction is not None:
            _do(transaction)
        else:
            with self.engine.begin() as trans:
                _do(trans)

    def hasIncomingChanges(self, remoteTable: OdkxServerTable) -> bool:
        """
        :param remoteTable:
        :return: true if there are changes on the server that have not been downloaded yet. use the "sync" function to download these changes
        """
        return remoteTable.getdataETag() != self.getLocalDataETag()

    def _sync_iter_pull(self, remoteTable: OdkxServerTable, no_attachments: bool = False):
        if remoteTable.getdataETag() == self.getLocalDataETag():
            ## we still need to check if we need to download attachments
            self._sync_pull_attachments(remoteTable)
            return False
        self._storage._cache_table_defintion(remoteTable.getTableDefinition())
        new_etag = self.stageAllDataChanges(remoteTable)
        st = self._getStagingTable()
        colnames = [x.name for x in st.columns]

        # sync up data
        with self.engine.begin() as trans:
            trans.execute("delete from {schema}.{table} where id in (select id from {schema}.{stagingtable})".format(
                schema=self.schema, table=self.tableId, stagingtable=self.tableId + '_staging'
            ))
            fields = ','.join(['"{colname}"'.format(colname=colname) for colname in colnames])
            fields_v = ','.join(['st."{colname}"'.format(colname=colname) for colname in colnames])
            trans.execute("""insert into {schema}.{table} ({fields},state) select {fields_v}, 'sync_attachments' as state from {schema}.{stagingtable} st
            inner join 
            ( 
            select l.id, max(l."rowETag") as "rowETag" from {schema}.{stagingtable} l inner join
            (select id, max("savepointTimestamp") as "savepointTimestamp" from {schema}.{stagingtable} group by id) latest_timestamp 
            on l.id = latest_timestamp.id and l."savepointTimestamp" = latest_timestamp."savepointTimestamp" group by l.id
            ) latest
            on latest.id = st.id and 
            latest."rowETag" = st."rowETag"
            """.format(
                schema=self.schema, table=self.tableId, stagingtable=self.tableId+'_staging', fields=fields, fields_v=fields_v
            ))
            self._staging_to_log(trans)
            self.updateLocalStatusDb(new_etag, trans)
        if not no_attachments:
            self._sync_pull_attachments(remoteTable)
        return True


    def _qryState(self, local_changes_prefix: str, tableDefinition: List[OdkxServerColumnDefinition], state: List[str], force_push: bool):
        locChanges = self._getTableMeta(self.tableId + '_' + local_changes_prefix)
        locTable = self._getTableMeta(self.tableId)
        locChangesCols = [x.name for x in locChanges.columns]
        locTableCols = [x.name for x in locTable.columns]
        colsTakeLocally = [x.elementKey for x in tableDefinition if x.isMaterialized()] \
                          + ['id', 'rowETag', 'savepointTimestamp', 'dataETagAtModification', 'savepointCreator', 'formId','savepointType', 'lastUpdateUser']
        if force_push:
            # take row ETag directly from server, making push always work even if we updated old data
            # it can still conflict but now only because somebody uploaded between us pulling and us pushing
            colsTakeLocally = [x for x in colsTakeLocally if not x in ['rowETag']]

        colsTakeLocally = [x for x in locChangesCols if x in colsTakeLocally]
        col_list = ['l."{x}"'.format(x=x) if x in colsTakeLocally else 'r."{x}"'.format(x=x) for x in locTableCols]
        qry = "SELECT {col_list} FROM {schema}.{loctable} l LEFT OUTER JOIN {schema}.{table} r ON l.id = r.id WHERE l.state in ({state})".format(
            schema=self.schema,
            loctable=self.tableId + '_' + local_changes_prefix,
            table=self.tableId,
            col_list=','.join(col_list),
            state=','.join(["'" + x + "'" for x in state])
        )
        return qry

    def row2rec(self,row: dict, definition: List[OdkxServerColumnDefinition], default_user: str):
        datacols = [x.elementKey for x in definition if x.isMaterialized()]
        ## TODO refactor
        for c in datacols:
            if not c in row.keys():
                raise Exception("schema's have diverged: on ODKX server i got column {c} but i couldn't find it locally. please fix.".format(c=c))
        tupColAccess = ('defaultAccess',  'groupModify', 'groupPrivileged', 'groupReadOnly', 'rowOwner')
        tupColnames = tuple(datacols)
        filterScope = {}
        orderedColumns = []
        for c in tupColAccess:
            if (c == 'defaultAccess' and row[c] is None):
                filterScope[c] = 'FULL'
            else:
                filterScope[c] = row[c]
        for c in tupColnames:
            orderedColumns.append({'column':c,'value':row[c]})

        result = {}
        result['filterScope'] = filterScope
        result['orderedColumns'] = orderedColumns
        fix_row_fields = ['createUser', 'lastUpdateUser', 'dataETagAtModification', 'rowETag', 'savepointCreator',
                          'formId', 'locale', 'savepointType', 'savepointTimestamp', 'deleted', 'id']
        for k in [ x for x in row.keys() if x in fix_row_fields]:
            if (k == 'savepointTimestamp' and row[k] is None):
                result[k] = str(datetime.datetime.now())
            elif (k == 'savepointTimestamp' and isinstance(row[k], datetime.datetime)):
                result[k] = str(row[k])
            elif (k == 'savepointType' and row[k] is None):
                result[k] = 'COMPLETE'
            elif (k in ('createUser','lastUpdateUser', 'savepointCreator') and row[k] is None):
                result[k] = default_user
            else:
                result[k] = row[k]
        return result

    def sync(self, remoteTable: OdkxServerTable, local_changes_prefix: Optional[str] = None, force_push: bool = False, no_attachments: bool = False):
        """

        :param remoteTable: the OdkxServerTable you want to sync with
        :param local_changes_prefix: the prefix of the local changes to push (when left empty , it will not push, only pull)
        :param force_push: if the server has more recent changes than our local changes, push anyway, overwriting the changes on the server
        :param no_attachments: ignore the attachments for now (the rows will remain in sync_attachments state, so they will be synced next time when you don't pass no_attachments)
        :return:
        """
        self._sync_iter_pull(remoteTable)
        definition = remoteTable.getTableDefinition().columns
        id_list_good = []
        id_list_conflict = []
        if local_changes_prefix is not None:
            if (self.hasUnresolvedConflicts(local_changes_prefix)):
                raise Exception("unresolved conflicts, cannot push changes")
            state_qry = self._qryState(local_changes_prefix, tableDefinition=definition, state=['new', 'modified'], force_push=force_push)
            records = []
            with self.engine.connect() as c:
                for row in c.execute(state_qry):
                    records.append(self.row2rec(row, definition, remoteTable.connection.user))
            json = {'rows': records, 'dataETag': self.getLocalDataETag()}
            if (len(records) == 0):
                return None
            #return json
            rs = remoteTable.alterDataRows(json)
            for outcome in rs['rows']:
                if outcome['outcome'] == 'IN_CONFLICT':
                    id_list_conflict.append(outcome['id'])
                else:
                    id_list_good.append(outcome['id'])
            def chunks(l, n):
                """Yield successive n-sized chunks from l."""
                for i in range(0, len(l), n):
                    yield l[i:i + n]
            for chunk in chunks(id_list_good, 10):
                qry = """update {schema}.{localtable} set state='synced' where id in ({ids})""".format(
                    schema=self.schema,
                    localtable=self.tableId+'_' + local_changes_prefix,
                    ids=','.join(["'{c}'".format(c=c) for c in chunk])
                )
                with self.engine.begin() as c:
                    c.execute(qry)
            for chunk in chunks(id_list_conflict, 10):
                qry = """update {schema}.{localtable} set state='conflict' where id in ({ids})""".format(
                    schema=self.schema,
                    localtable=self.tableId+'_' + local_changes_prefix,
                    ids=','.join(["'{c}'".format(c=c) for c in chunk])
                )
                with self.engine.begin() as c:
                    c.execute(qry)
            self._sync_iter_pull(remoteTable, no_attachments=no_attachments)
            return rs



    def _getTableMeta(self, tablename: str) -> sqlalchemy.Table:
        meta = sqlalchemy.MetaData()
        meta.reflect(self.engine, schema=self.schema, only=[tablename])
        return meta.tables.get(self.schema+ '.' + tablename)


    def _getHashedColumns(self, table_name):
        tm = self._getTableMeta(table_name)
        exclude_columns = ['hash', 'state', 'dataETagAtModification', 'formId', 'rowETag', 'savepointTimestamp',
                           'savepointCreator',
                           'createUser', 'lastUpdateUser', 'locale', 'savepointType']
        columns_to_hash = [x.name for x in tm.columns
                           if not x.name in exclude_columns]
        return columns_to_hash

    def fillHashColumn(self, table_name):
        columns_to_hash = self._getHashedColumns(table_name)
        qry = """UPDATE {schema}.{table} set hash=md5(ROW({cols})::TEXT)""".format(
            schema=self.schema,
            table=table_name,
            cols=','.join(['"{c}"'.format(c=c) for c in columns_to_hash]))
        with self.engine.begin() as c:
            c.execute(qry)

    def _copyMissingData(self, tn1, tn2):
        t1 = self._getTableMeta(tn1)
        t2 = self._getTableMeta(tn2)
        t1_cols = [x.name for x in t1.columns]
        t2_cols = [x.name for x in t2.columns]
        common_cols = [x for x in t1_cols if x in t2_cols]
        qry = """
            INSERT INTO {schema}."{tn2}" ({cols}) select {cols} from {schema}."{tn1}" where not {schema}."{tn1}".id in (select id from {schema}."{tn2}")
        """.format(schema=self.schema, tn1= tn1, tn2 = tn2, cols=",".join(['"{c}"'.format(c=x) for x in common_cols]))
        with self.engine.begin() as c:
            c.execute(qry)

    def _fillUUIDs(self, tn, uuid_col):
        qry = """
            UPDATE {schema}."{tn}" set "{uuid_col}"=md5(random()::text || clock_timestamp()::text)::uuid where "{uuid_col}" is null
        """.format(schema=self.schema, tn=tn, uuid_col=uuid_col)
        with self.engine.begin() as c:
            c.execute(qry)

    def resetColumns(self, table: str, col_list: List[str],external_id: str):
        """ reset the given columns in this table from the latest version (eg you blanked them and you don't want the blanked version to be uploaded)
        warning: external_id needs to be a UNIQUE field!
        """
        col_expr = ",".join([
            """"{c}" = {schema}."{master}"."{c}" """.format(schema=self.schema, tn=table, master=self.tableId, c=x)
            for x in col_list])

        qry = """
            UPDATE {schema}."{tn}" set {col_expr} FROM {schema}."{master}" WHERE {schema}."{tn}"."{extid}" = {schema}."{master}"."{extid}"
        """.format(
            schema=self.schema,
            master = self.tableId,
            tn = table,
            extid = external_id,
            col_expr = col_expr
        )
        with self.engine.begin() as c:
            c.execute(qry)

    def localSyncFromDataframe(self, source_prefix: str, external_id_column: str, df: pd.DataFrame, localSyncMode: LocalSyncMode = LocalSyncMode.FULL):
        """
        to sync changes from a dataframe:
          * first do initializeExternalSource
          * then do this function, with the same prefix. give a UNIQUE column as external_id (if you use the odkx ID then just pass 'id')
          * then do sync with the remote table and give the external source prefix as a parameter

        this will stage changes for syncing from the given database. note that every column missing from the database will NOT BE TOUCHED
        (it uses resetColumns to reset all columns that were not in the dataframe).


        note that if you didn't sync up the pending changes and you want to re-do the local sync, you must resetLocalChanges first (a localSync
        won't work when there are already changes pending)

        :param source_prefix: the prefix of the externalSource
        :param external_id_column: the "ID" you want to use as primary key for this operation. if you just want to use the ODKX id, pas "id"
        :param df: a dataframe containing at least the external_id_column and then one or more columns that also appear in the ODKX table.
        :param localSyncMode: FULL or ONLY_NEW_RECORDS. when ONLY_NEW_RECORDS then modifications will not be synced only additions.
        :return:
        """
        staging_tn = self.tableId + '_' + source_prefix + '_staging'
        hash_cols = self._getHashedColumns(staging_tn)
        missing_cols = [x for x in hash_cols if not x in list(df)]
        qry = """DELETE FROM {schema}."{tn}" """.format(schema=self.schema, tn=staging_tn)
        with self.engine.begin() as c:
            c.execute(qry)
        df.to_sql(staging_tn, schema=self.schema, if_exists='append', index=False, con=self.engine)
        self.resetColumns(staging_tn, missing_cols, external_id_column)
        self.localSyncFromStagingTable(source_prefix, external_id_column, localSyncMode)

    def hasPendingLocalChanges(self, source_prefix: str):
        def_tn = self.tableId + '_' + source_prefix
        q_test = """
        select count(id) as aantal from {schema}."{deftn}" where not state in ('unchanged', 'synced')
        """.format(schema=self.schema, deftn=def_tn)
        with self.engine.connect() as c:
            res = c.execute(q_test)
            for r in res:
                if r['aantal'] > 0:
                    return True
        return False

    def hasUnresolvedConflicts(self, source_prefix: str):
        def_tn = self.tableId + '_' + source_prefix
        q_test = """
        select count(id) as aantal from {schema}."{deftn}" where state in ('conflict')
        """.format(schema=self.schema, deftn=def_tn)
        with self.engine.connect() as c:
            res = c.execute(q_test)
            for r in res:
                if r['aantal'] > 0:
                    return True
        return False


    def resetLocalChanges(self, source_prefix: str):
        def_tn = self.tableId + '_' + source_prefix
        q_test = """
        delete from {schema}."{deftn}"
        """.format(deftn=def_tn,schema=self.schema)
        with self.engine.begin() as c:
            c.execute(q_test)



    def localSyncFromStagingTable(self, source_prefix: str, external_id_column: str, localSyncMode: LocalSyncMode = LocalSyncMode.FULL):
        """
        DO NOT USE THIS FUNCTION if you are writing an interactive editing application.
        this function takes the sync time as edit time, which is not right for editing apps

        an editing app should do the following when saving a record:
         * taking the record from the master table and making modifications
         * setting savepointTimestamp / lastUpdateUser / savepointType accordingly
         * delete old record from the externalsource table
         * replace it by the updated record

        this is only for batch syncing with systems that don't know about record versions and edit timestamps

        it will consider the following tables:
         * [tableId]_[externalsource]_staging --> the staging table. just dump data there (to create a staging table, see SqlLocalStorage.initializeExternalSource)
         * [tableId]_[externalsource] --> the table containing the modifications coming from externalsource

        this function will compare the staging with the modif tables, apply changes to the modif table while setting the relevant metadata fields
        when an update happens on the odkX server on a record after the record has been modified by the localSync, a conflict will arise and the sync
        will fail. to prevent this, one can use force_push=True while syncing

        the table t_prefix contains the state after the previous sync. the staging table (t_prefix_staging) is
        compared with this table to determine what changed.
        :param source_prefix: the name of the external source (use lowercase no spaces)
        :param external_id_column: the primary key field of a record IN THE EXTERNAL SYSTEM (so not the odkx ID field)
        :return:
        """

        staging_tn = self.tableId + '_' + source_prefix + '_staging'
        def_tn = self.tableId + '_' + source_prefix
        self._copyMissingData(self.tableId, def_tn)
        self.fillHashColumn(def_tn)

        if (self.hasPendingLocalChanges(source_prefix)):
            raise Exception("unsynced local changes still pending. sync first")


        qry = """
            UPDATE {schema}."{stagingtable}" set id = {schema}."{realtable}".id, state='modified', "rowETag" = {schema}."{realtable}"."rowETag"
            FROM {schema}."{realtable}" WHERE {schema}."{stagingtable}"."{extid}" = {schema}."{realtable}"."{extid}"
        """
        with self.engine.begin() as c:
            c.execute("update {schema}.{stagingtable} set state=null".format(schema=self.schema, stagingtable=staging_tn))
            c.execute("update {schema}.{stagingtable} set deleted=False where deleted is null".format(schema=self.schema, stagingtable=staging_tn))
            c.execute(qry.format(schema=self.schema, stagingtable=staging_tn, realtable=def_tn, extid=external_id_column))
            c.execute(qry.format(schema=self.schema, stagingtable=staging_tn, realtable=self.tableId, extid=external_id_column))

        self.fillHashColumn(staging_tn)
        qry = """
            UPDATE {schema}."{stagingtable}" set "rowETag" = {schema}."{realtable}"."rowETag", state='unchanged'
            FROM {schema}."{realtable}" WHERE {schema}."{stagingtable}".id = {schema}."{realtable}".id AND 
            {schema}."{stagingtable}".hash = {schema}."{realtable}".hash
        """
        with self.engine.begin() as c:
            c.execute(qry.format(schema=self.schema, stagingtable=staging_tn, realtable=def_tn))
            c.execute("""update {schema}."{stagingtable}" set state='new', "createUser" = 'localSync'
                        where state is null""".format(schema=self.schema, stagingtable=staging_tn))
            c.execute("""update {schema}."{stagingtable}" set "savepointTimestamp"=now(), 
                        "savepointCreator"='localSync', 
                        "savepointType"='COMPLETE',
                        "formId"='localSync',
                        "lastUpdateUser"='localSync'
                        where state in ('new', 'modified')""".format(schema=self.schema, stagingtable=staging_tn))
            c.execute("""update {schema}."{stagingtable}" set "dataETagAtModification"='{etag}' where state in ('new', 'modified')""".format(schema=self.schema, stagingtable=staging_tn, etag=self.getLocalDataETag()))

        self._fillUUIDs(staging_tn, 'id')
        self._fillUUIDs(staging_tn, 'rowETag')

        with self.engine.begin() as c:
            w = ""
            if localSyncMode == LocalSyncMode.ONLY_NEW_RECORDS:
                w = " WHERE state in ('new') "
            if localSyncMode == LocalSyncMode.ONLY_EXISTING_RECORDS:
                w = " WHERE state in ('modified') "
            c.execute("""delete from {schema}."{def_tn}" where {schema}."{def_tn}".id in (select {schema}."{stagingtable}".id from {schema}."{stagingtable}" {w}) 
            """.format(schema=self.schema, def_tn=def_tn, stagingtable=staging_tn, w=w))
        self._copyMissingData(staging_tn, def_tn)

