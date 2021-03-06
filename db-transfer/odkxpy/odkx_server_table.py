from .odkx_server_file import OdkxServerFile
import json
from collections import namedtuple
from .odkx_connection import OdkxConnection
import datetime
import logging
from typing import List, Generator, NamedTuple, Union

OdkxServerTableInfo = namedtuple('OdkxServerTableInfo', [
    'tableId', 'dataETag', 'schemaETag', 'selfUri', 'definitionUri', 'dataUri', 'instanceFilesUri', 'diffUri', 'aclUri', 'tableLevelManifestETag'
])

OdkxServerTableInfo.__new__.__defaults__ = (
    None, ) * len(OdkxServerTableInfo._fields)


OdkxServerTableRowset = namedtuple('OdkxServerTableRowset',
                                   ['rows', 'dataETag', 'tableUri', 'webSafeRefetchCursor', 'webSafeBackwardCursor',
                                       'webSafeResumeCursor', 'hasMoreResults', 'hasPriorResults']
                                   )

OdkxServerTableColumn = namedtuple(
    'OdkxServerTableColumn', ['column', 'value'])


class RowFilterScope(NamedTuple):
    defaultAccess: str  # FULL, MODIFY, READ_ONLY, HIDDEN,
    rowOwner: str
    groupReadOnly: str
    groupModify: str
    groupPrivileged: str


class OdkxServerTableRow(NamedTuple):
    rowETag: str
    dataETagAtModification: str
    deleted: bool
    createUser: str
    lastUpdateUser: str
    formId: str
    locale: str
    savepointType: str
    savepointTimestamp: datetime.datetime
    savepointCreator: str
    orderedColumns: List[OdkxServerTableColumn]
    selfUri: str
    id: str
    filterScope: RowFilterScope


class OdkxServerColumnDefinition(object):
    def __init__(self, elementKey=None, elementName=None, elementType=None, childElements: list = [], parentElement=None):
        if elementKey is None:
            raise ValueError("elementKey can not be None")

        self.elementKey = elementKey
        self.elementName = elementName
        self.elementType = elementType
        self.childElements = childElements
        self.parentElement = parentElement
        self.properties = {}

    def isMaterialized(self) -> bool:
        """
        :return: true if this column will be represented physically in a table
        """
        if self.parentElement is not None and self.parentElement.elementType == 'array':
            return False
        if len(self.childElements) > 0:
            if self.elementType == 'array':
                return True
            else:
                return False
        return True

    def _serialization_helper(self):
        return {
            "elementKey": self.elementKey,
            "elementName": self.elementName,
            "elementType": self.elementType,
            "properties": self.properties,
            "listChildElementKeys": [child.elementKey for child in self.childElements]
        }

    def __repr__(self):
        rpr = (' - ' if self.parentElement is not None else '') + 'OdkxServerColumnDefinition' + \
            ('*' if self.isMaterialized() else '') + \
            '(' + self.elementKey + ':' + self.elementType + ')'
        for p in self.properties.keys():
            sv = str(self.properties[p])
            if len(sv) > 40:
                sv = sv[:37]+'...'
            rpr += '\n\t{k}={v}'.format(k=p, v=sv)
        return rpr


class OdkxServerTableDefinition():
    """
    getTableDefintion result
    """

    def __init__(self,  schemaETag: str, tableId: str, columns: List[OdkxServerColumnDefinition]):
        self.schemaETag = schemaETag
        self.tableId = tableId
        self.columns = columns

    def _asdict(self):
        """
        dict for serialization
        """

        json = {}
        json["schemaETag"] = str(self.schemaETag)
        json["tableId"] = self.tableId
        json["orderedColumns"] = [obj._serialization_helper()
                                  for obj in self.columns]
        return json

    @classmethod
    def _extract(cls, obj) -> "OdkxServerTableDefinition":
        cols = None
        if isinstance(obj, OdkxServerTable):
            return obj.getTableDefinition()
        elif isinstance(obj, dict):
            return cls._from_cache(obj)
        
        raise ValueError("could not extract def from {}".format(str(obj)))

    @classmethod
    def tableDefinitionOf(cls, odx_table: Union[dict, "OdkxServerTable"]) -> "OdkxServerTableDefinition":
        return cls._extract(odx_table)

    @classmethod
    def _from_cache(cls, obj) -> "OdkxServerTableDefinition":
        cols = {}
        for c in obj["orderedColumns"]:
            dd = {}
            dd.update(c)
            del dd['listChildElementKeys']
            cd_props = dd.pop('properties')
            cd = OdkxServerColumnDefinition(**dd)
            cd.properties = cd_props

            cols[cd.elementKey] = cd

        for c in obj['orderedColumns']:
            children = c['listChildElementKeys']
            parent = cols[c['elementKey']]
            for c in children:
                cols[c].parentElement = parent
                parent.childElements.append(cols[c])

        deflist = [cols[x["elementKey"]] for x in obj["orderedColumns"]]

        return OdkxServerTableDefinition(obj["schemaETag"], obj["tableId"],deflist)


# OdkxServerTableDefinition = namedtuple('OdkxServerTableDefinition', [
#     'schemaETag', 'tableId', 'orderedColumns', 'selfUri', 'tableUri'
# ])


class OdkxServerTable(object):
    """
    wrapper around the ODKX sync endpoint rest API for one specific table.
    to get a table, use OdkxServerMeta
    """

    def __init__(self, con: OdkxConnection, tableId: str, schemaETag: str):
        self.connection = con
        self.tableId = tableId
        self.schemaETag = schemaETag

    def getTableInfo(self) -> OdkxServerTableInfo:
        return OdkxServerTableInfo(**self.connection.GET('tables/' + self.tableId))

    def getFileManifest(self) -> List[OdkxServerFile]:
        return [OdkxServerFile(**x) for x in self.connection.GET("manifest/2/" + self.tableId)['files']]

    def getFile(self, path):
        return self.connection.GET('files/2/tables/' + self.tableId + ('' if path.startswith('/') else '/') + path)

    def putFile(self, content_type, payload, path):
        headers = {"Content-Type": content_type}
        if type(payload) in(dict, list):
            payload = json.dumps(payload).encode('utf-8')
        if type(payload) == str:
            payload = payload.encode('utf-8')
        return self.connection.POST('/files/2/tables/' + self.tableId + ('' if path.startswith('/') else '/') + path,
                                    headers=headers, data=payload)

    def deleteFile(self, path):
        return self.connection.DELETE('files/2/tables/' + self.tableId + (
            '' if path.startswith('/') else '/') + path)

    def getdataETag(self):
        return self.getTableInfo().dataETag

    def getTableRoot(self):
        return "tables/" + self.tableId + "/ref/" + self.schemaETag

    def getTableDefinition(self) -> OdkxServerTableDefinition:
        t_d = self.connection.GET(self.getTableRoot())
        col_props = [x for x in self.connection.GET(
            "tables/" + self.tableId + "/properties/2") if x['partition'] == 'Column']
        etag = t_d["schemaETag"]
        t_id = self.tableId

        cols = {}
        for c in t_d['orderedColumns']:
            dd = {}
            dd.update(c)
            del dd['listChildElementKeys']
            cd = OdkxServerColumnDefinition(**dd)
            cd.childElements = []
            for prop in [x for x in col_props if x['aspect'] == cd.elementKey]:
                cd.properties[prop['key']] = prop['value']

            cols[cd.elementKey] = cd
        for c in t_d['orderedColumns']:
            children = json.loads(c['listChildElementKeys'])
            parent = cols[c['elementKey']]
            for c in children:
                cols[c].parentElement = parent
                parent.childElements.append(cols[c])
        deflist = [cols[x['elementKey']] for x in t_d['orderedColumns']]

        return OdkxServerTableDefinition(etag, t_id, deflist)

    # def setTableDefinition(self, json):
    #    return self.connection.PUT(self.getTableRoot(), json)

    def deleteTable(self, are_you_sure: bool):
        """To delete a table
        """
        if not are_you_sure:
            raise Exception("not sure ?")
        return self.connection.DELETE(self.getTableRoot())

    def getTableProperties(self):
        return self.connection.GET('tables/' + self.tableId + "/properties/2")

    def putJsonTableProperties(self, json):
        return self.connection.PUT('tables/' + self.tableId + "/properties/2", json)

    def getTableAcl(self):
        return self.connection.GET('tables/' + self.tableId + '/acl')

    def _parse_rowset(self, r):
        r['rows'] = [self._parse_row(x) for x in r['rows']]
        d = OdkxServerTableRowset(**r)
        return d

    def _parse_row(self, r):
        def rw(x):
            x['orderedColumns'] = [OdkxServerTableColumn(
                **z) for z in x['orderedColumns']]
            fs = x['filterScope']
            x['filterScope'] = RowFilterScope(**fs)
            return x
        return OdkxServerTableRow(**rw(r))

    def _generator_rowset(self, l) -> Generator[OdkxServerTableRowset, None, None]:
        hasmore = True
        cursor = None
        while hasmore:
            rs = l(cursor)
            hasmore = rs.hasMoreResults
            cursor = rs.webSafeResumeCursor
            yield rs

    def getDiffGenerator(self, dataETag=None, fetchLimit=None) -> Generator[OdkxServerTableRowset, None, None]:
        return self._generator_rowset(
            lambda z_cursor: self.getDiff(dataETag=dataETag, cursor=z_cursor, fetchLimit=fetchLimit))

    def getDiff(self, dataETag=None, cursor=None, fetchLimit=None) -> OdkxServerTableRowset:
        params = {'data_etag': dataETag,
                  'cursor': cursor, 'fetchLimit': fetchLimit}
        r = self.connection.GET(self.getTableRoot() + "/diff", params)
        return self._parse_rowset(r)

    def getAllDataRowsGenerator(self, fetchLimit=None) -> Generator[OdkxServerTableRowset, None, None]:
        return self._generator_rowset(
            lambda z_cursor: self.getAllDataRows(cursor=z_cursor, fetchLimit=fetchLimit))

    def getAllDataRows(self, cursor=None, fetchLimit=None) -> OdkxServerTableRowset:
        params = {'cursor': cursor, 'fetchLimit': fetchLimit}
        return self._parse_rowset(self.connection.GET(self.getTableRoot() + "/rows", params))

    def getChangesets(self, dataETag=None, sequence_value=None):
        # Not working - Problem API ?
        # todo refactor after ludovic explains me
        params = {'data_etag': dataETag, 'sequence_value': sequence_value}
        return self.connection.GET(self.getTableRoot() + "/diff/changeSets", params)

    def getChangesetRows(self, dataETag, cursor=None, fetchLimit=None, active_only=None):
        # todo refactor after ludovic explains me
        params = {'active_only': active_only,
                  'cursor': cursor, 'fetchLimit': fetchLimit}
        return self.connection.GET(self.getTableRoot() + "/diff/changeSets/" + dataETag, params)

    def getDataRow(self, rowId, raw=False):
        r = self.connection.GET(self.getTableRoot() + "/rows/" + rowId)
        if raw:
            return r
        return self._parse_row(r)

    def getAttachmentsManifest(self, rowId):
        return [OdkxServerFile(**d) for d in self.connection.GET(self.getTableRoot() + "/attachments/" + rowId + "/manifest")['files']]

    # I GOT HERE REFACTORING

    def getAttachment(self, rowId, name, stream, timeout):
        return self.connection.session.get(
            self.connection.server + self.connection.appID + '/' +
            self.getTableRoot() + "/attachments/" + rowId + "/file/" + name,
            stream=stream, timeout=timeout)

    def getAttachments(self, rowId, data):
        # Not working - TODO
        # @ludovic i think this is probably setAttachments ? @TODO
        headers = {"Content-Type": "application/json"}
        payload = json.dumps(data)
        return self.session.post(
            self.server + self.appID + '/' + self.getTableRoot() + "/attachments/" +
            rowId + "/download",
            headers=headers, data=payload)

    def alterDataRows(self, json):
        """Insert, Update or Delete"""
        return self.connection.PUT(self.getTableRoot() + "/rows", json)

    # Manipulate individual records

    def addRecord(self, dataETag, formId, **kwargs):
        orderedColumns = []
        for key, item in kwargs.items():
            orderedColumns.append({'column': key, 'value': item})
        json = {'rows':
                [{
                    'rowETag': None,
                    'dataETagAtModification': None,
                    'deleted': False,
                    'createUser': self.user,
                    'lastUpdateUser': self.user,
                    'formId': formId,
                    'savepointTimestamp': str(datetime.datetime.now()),
                    'savepointCreator': self.user,
                    'orderedColumns': orderedColumns
                }],
                'dataETag': dataETag}
        output = self.alterDataRows(json)
        return output

    def alterRecord(self, dataETag, rowId, **kwargs):
        onerow = self.getDataRow(rowId, raw=True)
        del onerow['selfUri']
        onerow['savepointTimestamp'] = str(datetime.datetime.now())
        onerow['savepointCreator'] = self.user
        orderedColumns = []
        for key, item in kwargs.items():
            orderedColumns.append({'column': key, 'value': item})
        onerow['orderedColumns'] = orderedColumns
        json = {'rows': [onerow], 'dataETag': dataETag}
        output = self.alterDataRows(json)
        return output

    def deleteRecord(self, dataETag, rowId):
        onerow = self.getDataRow(rowId, raw=True)
        del onerow['selfUri']
        onerow['deleted'] = True
        json = {'rows': [onerow], 'dataETag': dataETag}
        output = self.alterDataRows(json)
        return output

    # dict needed to manipulate records

    def dictAddRecord(self, formId, kwargs):
        orderedColumns = []
        for key, item in kwargs.items():
            orderedColumns.append({'column': key, 'value': item})
        dict_ = {
            'rowETag': None,
            'dataETagAtModification': None,
            'deleted': False,
            'createUser': self.user,
            'lastUpdateUser': self.user,
            'formId': formId,
            'savepointTimestamp': str(datetime.datetime.now()),
            'savepointCreator': self.user,
            'orderedColumns': orderedColumns
        }
        return dict_

    def dictAlterRecord(self, rowId, kwargs):
        onerow = self.getDataRow(rowId)
        del onerow['selfUri']
        onerow['savepointTimestamp'] = str(datetime.datetime.now())
        onerow['savepointCreator'] = self.user
        orderedColumns = []
        for key, item in kwargs.items():
            orderedColumns.append({'column': key, 'value': item})
        onerow['orderedColumns'] = orderedColumns
        return onerow

    def dictDeleteRecord(self, rowId):
        onerow = self.getDataRow(rowId)
        del onerow['selfUri']
        onerow['deleted'] = True
        return onerow

    # Manipulate records

    def addRecords(self, dataETag, formId, lst_kwargs):
        lst_entry = []
        for kwargs in lst_kwargs:
            lst_entry.append(self.dictAddRecord(formId, kwargs))
        json = {'rows': lst_entry, 'dataETag': dataETag}
        return self.alterDataRows(json)

    def alterRecords(self, dataETag, lst_rowId, lst_kwargs):
        lst_entry = []
        for rowId, kwargs in zip(lst_rowId, lst_kwargs):
            lst_entry.append(self.dictAlterRecord(rowId, kwargs))
        json = {'rows': lst_entry, 'dataETag': dataETag}
        return self.alterDataRows(json)

    def deleteRecords(self, dataETag, lst_rowId):
        lst_entry = []
        for rowId in lst_rowId:
            lst_entry.append(self.dictDeleteRecord(rowId))
        json = {'rows': lst_entry, 'dataETag': dataETag}
        return self.alterDataRows(json)

    def addAlterDeleteRecords(self, dataETag, formId, local_records, remote_records):
        lst_entry = []
        remoteIDs = [x['id'] for x in remote_records]
        localIDs = [x['id'] for x in local_records]

        for item in local_records:
            if item['id'] not in remoteIDs:  # Add
                #                lst_entry.append(self.dictAddRecord(formId, item))
                lst_entry.append(item)
            elif item['id'] in remoteIDs:  # Alter
                #                lst_entry.append(self.dictAlterRecord(item['id'], item))
                lst_entry.append(item)

        # TODO Delete with the delete field
        # for item in remote_records:
        #    if item['id'] not in localIDs:  # Delete
        #        lst_entry.append(self.dictDeleteRecord(item['id']))

        json = {'rows': lst_entry, 'dataETag': dataETag}
        res = self.alterDataRows(json)
        return res

    def getRecords(self, dataETag, lst_rowId):
        lst_entry = []
        for rowId in lst_rowId:
            onerow = self.getDataRow(rowId)
            del onerow['selfUri']
            lst_entry.append(onerow)
        json = {'rows': lst_entry, 'dataETag': dataETag}
        return json

    # Sync process components

    def compareDataETag(self, dataETagLocal):
        dataETag = self.getTableInfo().dataETag
        if dataETag == dataETagLocal:
            logging.info("same dataETag: " + dataETag)
            return True
        logging.info("different dataETag: " +
                     str(dataETag) + ', ' + dataETagLocal)
        return False

    def getAllResults(self, mode, dataETag=None):
        if mode == "AllDataRows":
            dict_ = self.getAllDataRows()
        elif mode == "AllDataChanges":
            dict_ = self.getAllDataChanges(dataETag=dataETag)

        notFinished = dict_['hasMoreResults']
        cursor = dict_['webSafeResumeCursor']
        while notFinished:
            logging.info("more rows ...")
            if mode == 'AllDataChanges':
                moreRes = self.getAllDataChanges(
                    dataETag=dataETag, cursor=cursor)
            elif mode == 'AllDataRows':
                moreRes = self.getAllDataRows(cursor=cursor)
            notFinished = moreRes['hasMoreResults']
            cursor = moreRes['webSafeResumeCursor']
            dict_["rows"].extend(moreRes["rows"])
        del dict_['tableUri']
        del dict_['webSafeRefetchCursor']
        del dict_['webSafeBackwardCursor']
        del dict_['webSafeResumeCursor']
        del dict_['hasMoreResults']
        del dict_['hasPriorResults']
        return dict_

    def push(self, local_records, formId):
        logging.info("Pushing")
        dataETag = self.getTable()['dataETag']
        remote_records = self.getAllResults('AllDataRows')['rows']
        return self.addAlterDeleteRecords(dataETag, formId, local_records, remote_records)

    def pull(self, dataETagLocal):
        logging.info("Pulling")
        return self.getAllResults('AllDataChanges', dataETag=dataETagLocal)

    # Sync process

    def tryPushOrPull(self, dataETagLocal, local_records, formId):
        """
        """
        if self.compareDataETag(dataETagLocal):
            logging.info("Update server")
            return self.push(local_records, formId)
        else:
            logging.info("Local client needs to be updated")
            return self.pull(dataETagLocal)

    def __repr__(self):
        return 'OdkxServerTable(' + self.tableId + ')'
