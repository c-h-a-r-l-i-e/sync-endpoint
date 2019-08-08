import odkxpy
import os
import time
import sqlalchemy
from sqlalchemy import create_engine, Table, Column, select, text, exists
import json

# For now hardcode sensitive databases we want to delete, could later make this some sort of configuration file
SENSITIVE_TABLES = ['survey', 'sample']
# Time we wait after transferring a set of rows before transferring the next set
WAIT_TIME = 15

TESTING = False

"""
 Provide this class with a OdkxServerTable and a sqlalchemy database engine

 It can transfer the information from this table to a table of the same name in the database
 (freshly created if doesn't already exist) and then optionally delete the records from the 
 ODK-X table
"""

class TableTransfer():
    """
    oldTable of type OdkxServerTable
    dbEng of sqlalchemy.Engine
    
    """
    def __init__(self, oldTable, dbEng):
        self.oldTable = oldTable
        self.dbEng = dbEng
        self.rows = []


    # Rows updated to current contents of the ODK table
    def updateRows(self):
        self.rows = self.oldTable.getAllDataRows().rows
        

    def getColumns(self):
        tableDef = self.oldTable.getTableDefinition()
        return tableDef.columns

    # Returns the name of all existing tables in the database
    def getDBTables(self):
        return self.dbEng.table_names()


    def getTableName(self):
        return self.oldTable.tableId


    def getColumnDatatype(self, col):
        dt = sqlalchemy.Text()
        if col.elementType == 'string':
            dt = sqlalchemy.Text()
        elif col.elementType == 'number':
            dt = sqlalchemy.Float()
        elif col.elementType == 'mimeType':
            dt = sqlalchemy.String(40)
        elif col.elementType == 'rowpath':
            dt = sqlalchemy.String(255)
        elif col.elementType == 'integer':
            dt = sqlalchemy.Integer()
        elif col.elementType == 'array':
            dt = sqlalchemy.types.JSON()
        return dt


    # Add column (from ODK-X) to sqlalchemy table
    def addColumn(self, col):
        cname = col.elementKey
        dt = self.getColumnDatatype(col)
        with self.dbEng.begin() as conn:
            conn.execute('ALTER TABLE {} ADD COLUMN "{}" {}'.format(self.getTableName(), cname, dt))


    # Create a database table which matches the ODK-X table
    def createDBTable(self):
        name = self.getTableName()
        if name in self.getDBTables():
            return # Exit as table already exists
        meta = sqlalchemy.MetaData()
        meta.bind = self.dbEng
        tbl = sqlalchemy.Table(name, meta) 

        # Add metadata columns
        tbl.append_column(sqlalchemy.Column('_id', sqlalchemy.String(50), primary_key=True, nullable=False))
        tbl.append_column(sqlalchemy.Column('_dataETagAtModification', sqlalchemy.String(50)))
        tbl.append_column(sqlalchemy.Column('_createUser', sqlalchemy.String(50)))

        # Add other columns
        for column in self.getColumns():
            cname = column.elementKey
            if not cname.startswith('log_'):
                dt = self.getColumnDatatype(column)
                tbl.append_column(sqlalchemy.Column(cname, dt))

        meta.create_all(self.dbEng)


    # Create a log table in the database for the ODK-X table
    def createDBLogTable(self):
        name = self.getTableName() + 'Log'
        if name in self.getDBTables():
            return # Exit as table already exists
        meta = sqlalchemy.MetaData()
        meta.bind = self.dbEng
        tbl = sqlalchemy.Table(name, meta) 

        # Add columns
        tbl.append_column(sqlalchemy.Column('log_id', sqlalchemy.Integer(), primary_key=True))
        tbl.append_column(sqlalchemy.Column('user', sqlalchemy.Text()))
        tbl.append_column(sqlalchemy.Column('time', sqlalchemy.String(100)))
        tbl.append_column(sqlalchemy.Column('row_id', sqlalchemy.String(50)))
        tbl.append_column(sqlalchemy.Column('row_dataETagAtModification', sqlalchemy.String(50)))
        tbl.append_column(sqlalchemy.Column('column', sqlalchemy.Text()))
        tbl.append_column(sqlalchemy.Column('new_data', sqlalchemy.Text()))

        meta.create_all(self.dbEng)


    # Copy rows to the database
    def copyRows(self):
        tableName = self.getTableName()
        if not tableName in self.getDBTables():
            print("Creating new table %s" % tableName)
            self.createDBTable()

        # Load table from the database
        meta = sqlalchemy.MetaData()
        table = Table(tableName, meta, autoload=True, autoload_with=self.dbEng)

        # Set up columns
        columnChange = False
        for col in self.getColumns():
            if not col.elementKey in [c.name for c in table.c]:
                if not col.elementKey.startswith('log_'):
                    print("Adding new column '%s' to table '%s'" % (col.elementKey, tableName))
                    self.addColumn(col)
                    columnChange = True
        
        # Reload table from the database if columns changed
        if columnChange:
            meta = sqlalchemy.MetaData()
            table = Table(tableName, meta, autoload=True, autoload_with=self.dbEng)

        conn = self.dbEng.connect()
        for row in self.rows:
            # Begin transaction to ensure this row is completely added or not at all added
            with conn.begin() as trans:
                # Check if row exists already
                query = select([table.c._dataETagAtModification]).where(table.c._id==row.id)
                result = conn.execute(query)

                #If row doesn't exist yet then add it
                if result.rowcount == 0:
                    print("Row does not exist, adding it...", end="")
                    values = {'_id': row.id, '_dataETagAtModification': row.dataETagAtModification, '_createUser': row.createUser}
                    for entry in row.orderedColumns:
                        if not entry.column.startswith('log_'):
                            values[entry.column] = entry.value

                    ins = table.insert().values(values)
                    conn.execute(ins)
                    print("Done")

                # Row already exists, so check if it is the same version
                else:
                    currentETag = result.fetchone()[0]
                    # If versions are different, update the entry in the database
                    if currentETag != row.dataETagAtModification:
                        print("Row has updates, updating now...", end="")
                        values = {'_id': row.id, '_dataETagAtModification': row.dataETagAtModification, '_createUser': row.createUser}
                        for entry in row.orderedColumns:
                            if not entry.column.startswith('log_'):
                                values[entry.column] = entry.value

                        update = table.update().where(table.c._id == row.id).values(values)
                        conn.execute(update)
                        print("done")

                result.close()

        conn.close()

    
    def getLogString(self, row):
        logMap = {}
        logCount = 0
        for col in row.orderedColumns:
            columnName = col.column
            if columnName.startswith('log_'):
                try:
                    logNum = int(columnName[4:])
                    if col.value != None:
                        logMap[logNum] = col.value
                        logCount = max(logCount, logNum + 1)

                except Exception:
                    print("WARNING: Invalid log column encountered: " + columnName)

        log = ""
        for i in range(logCount):
            if i in logMap.keys():
                log += logMap[i]
            else:
                print("WARNING: ODK table is missing log column: log_" + str(i))

        return log


    # Update on-database log 
    def updateLog(self):
        tableName = self.getTableName() + "Log"
        if not tableName in self.getDBTables():
            print("Creating new log table %s" % tableName)
            self.createDBLogTable()

        # Load table from the database
        meta = sqlalchemy.MetaData()
        table = Table(tableName, meta, autoload=True, autoload_with=self.dbEng)

        # Ensure all columns exist - we don't want any errors later on due to missing columns
        for col in ['user', 'time', 'row_id', 'column', 'new_data']:
            if not col in [c.name for c in table.c]:
                print("ERROR: log table doesn't contain column: " + col)
                return
        
        conn = self.dbEng.connect()
        for row in self.rows:
            # Begin transaction to ensure this row is completely added or not at all added
            with conn.begin() as trans:
                # Check if row exists already
                query = select([table.c.row_dataETagAtModification]).where(table.c.row_id==row.id)
                result = conn.execute(query)

                #If row doesn't exist yet or has been updated, then add its log
                oldRow = conn.execute(select([exists().where(table.c.row_id==row.id)
                    .where(table.c.row_dataETagAtModification == row.dataETagAtModification)])).scalar()

                if not oldRow:
                    print("Log for row has changed, adding them...", end="")
                    baseValues = {'row_id': row.id, 'row_dataETagAtModification': row.dataETagAtModification, 'user': row.lastUpdateUser}

                    #logEntries is a list of lists as follows: [[time1, columnName1, newData1], [time2, columnName2, newData2], ...]
                    logEntries = json.loads(self.getLogString(row))
                    for entry in logEntries:
                        if entry != None and len(entry) == 3:
                            time = entry[0]
                            column= entry[1]
                            newData = entry[2]

                            values = baseValues.copy()
                            values['time'] = time
                            values['column'] = column
                            values['new_data'] = str(newData)

                            ins = table.insert().values(values)
                            conn.execute(ins)

                    print("Done")

                result.close()

        conn.close()
            

    def deleteRows(self):
        for row in self.rows:
            try:
                self.oldTable.deleteRecord(row.dataETagAtModification, row.id)
            except Exception as err:
                print("Failed to delete row due to:" + err)


    def transfer(self, deleteAfter=None):
        if deleteAfter is None:
            deleteAfter = self.getTableName() in SENSITIVE_TABLES
        self.updateRows()
        self.copyRows()
        self.updateLog()
        if deleteAfter:
            self.deleteRows()


if __name__ == "__main__":
    # Get password from secret provided by docker
    con = odkxpy.OdkxConnection('http://nginx/odktables/', 'dbTransfer', 
            os.popen("cat /run/secrets/sync-pwd").read())

    if TESTING:
        con = odkxpy.OdkxConnection('http://odk.fr.to/odktables/', 'cmaclean', 
                'T35t')

    meta = odkxpy.OdkxServerMeta(con)
    tables = meta.getTables()

    if TESTING:
        eng = create_engine('postgresql://test:test@localhost/test')

    else:
        db = os.environ['POSTGRES_DB']
        pswd = os.environ['POSTGRES_PASSWORD']
        usr = os.environ['POSTGRES_USER']
        eng = create_engine('postgresql://{}:{}@frontend-db/{}'.format(usr, pswd, db))

    transfers = []
    for table in tables:
        transfers.append(TableTransfer(table, eng))
    while True:
        for transfer in transfers:
            transfer.transfer()
        time.sleep(WAIT_TIME)

        # Add any recently added tables and transfer them
        newTables = meta.getTables()
        for table in newTables:
            if not table.tableId in [t.tableId for t in tables]:
                print("Monitoring new table: " + table.tableId)
                tables.append(table)
                transfers.append(TableTransfer(table, eng))
