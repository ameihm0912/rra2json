#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# Copyright (c) 2015 Mozilla Corporation
# Contributors:
# Guillaume Destuynder <gdestuynder@mozilla.com>
# Gene Wood <gene@mozilla.com> (Authentication)

from oauth2client.client import SignedJwtAssertionCredentials
import gspread
import hjson as json
from xml.etree import ElementTree as et
import sys
import pytz
from datetime import datetime
from dateutil.parser import parse
from mozdef_client import mozdef_client as mozdef

class DotDict(dict):
    '''dict.item notation for dict()'s'''
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

    def __init__(self, dct):
        for key, value in dct.items():
            if hasattr(value, 'keys'):
                value = DotDict(value)
            self[key] = value

def fatal(msg):
    print(msg)
    sys.exit(1)

def debug(msg):
    sys.stderr.write('+++ {}\n'.format(msg))

def toUTC(suspectedDate, localTimeZone=None):
    '''Anything => UTC date. Magic.'''
    if (localTimeZone == None):
        try:
            localTimeZone = '/'.join(os.path.realpath('/etc/localtime').split('/')[-2:])
        except:
            localTimeZone = 'UTC'
    utc = pytz.UTC
    objDate = None
    if (type(suspectedDate) == str):
        objDate = parse(suspectedDate, fuzzy=True)
    elif (type(suspectedDate) == datetime):
        objDate=suspectedDate

    if (objDate.tzinfo is None):
        try:
            objDate=pytz.timezone(localTimeZone).localize(objDate)
        except pytz.exceptions.UnknownTimeZoneError:
            #Meh if all fails, I decide you're UTC!
            objDate=pytz.timezone('UTC').localize(objDate)
        objDate=utc.normalize(objDate)
    else:
        objDate=utc.normalize(objDate)
    if (objDate is not None):
        objDate=utc.normalize(objDate)

    return objDate

def post_rra_to_mozdef(cfg, rrajsondoc):
    msg = mozdef.MozDefRRA('{proto}://{host}:{port}/{rraindex}'.format(proto=cfg['proto'], host=cfg['host'],
        port=cfg['port'], rraindex=cfg['rraindex']))
    msg.set_fire_and_forget(False)
    msg.category = rrajsondoc.category
    msg.tags = rrajsondoc.tags
    msg.summary = rrajsondoc.summary
    msg.details = rrajsondoc.details
    msg.send()

def gspread_authorize(email, private_key, scope, secret=None):
    '''
    Authenticate to Google Drive and return an authorization.
    '''
    private_key = private_key.encode('ascii')
    if secret:
        credentials = SignedJwtAssertionCredentials(email, private_key, [scope], secret)
    else:
        credentials = SignedJwtAssertionCredentials(email, private_key, [scope])
    return gspread.authorize(credentials)

def get_sheet_titles(gc):
    '''
    List all sheets (Atom elements)
    '''
    data = {}
    et_sheets = gc.get_spreadsheets_feed()
    et_entries = et_sheets.findall('{http://www.w3.org/2005/Atom}entry')

    for et_entry in et_entries:
        # That's where the link with sheet ID always is, basically, since it's not named as such and there's several
        # links...
        link = et_entry.findall('{http://www.w3.org/2005/Atom}link')[1].attrib['href']
        #Links look like 'https://docs.google.com/spreadsheets/d/1nNhoENKv5qR6l_Ch2loYj0D9fQ_bNCz2pbHAEYssh-X/edit'
        #Where 1nNhoENKv5qR6l_Ch2loYj0D9fQ_bNCz2pbHAEYssh-X would be the ID
        linkid = link.split('/')[-2]
        # There's just one title so yay!
        title = et_entry.findall('{http://www.w3.org/2005/Atom}title')[0].text
        data[linkid] = title
    return data

def nodots(data):
    return data.replace('.', '')

def detect_version(gc, s):
    '''
    Find a sheet called Version and something that looks like a version number in cell 1,16 (P1)
    Else, we try to guess.
    '''

    # If the sheet is specifically marked as deprecated/etc, bail out now!
    if (s.sheet1.title.lower() in ['cancelled', 'superseded', 'deprecated', 'invalid']):
        return None

    # If we're lucky there's a version number (RRA format >2.4.1)
    version = s.sheet1.cell(1,16).value
    if version != '':
        return nodots(version)

    # so that's when we're not so lucky.
    #RRA 2.4.0 doesn't have the version number but has likelihood, and has a specific cell
    #It's nearly the same as RRA 2.4.1
    if (s.sheet1.cell(1,8).value == 'Estimated\nRisk to Mozilla'):
        version = '2.4.0'
        return nodots(version)

    #RRA 2.3 has a specific cell as well
    if (s.sheet1.cell(1, 8).value == 'Impact to Mozilla'):
        version = '2.3.0'
        return nodots(version)

    #RRA 1.x has a specific cell as well - getting monotonous here!
    if (s.sheet1.cell(1,1).value == 'Project Name' and s.sheet1.title == 'Summary'):
        version = '1.0.0'
        return nodots(version)

    # Out of luck.
    return None

def check_last_update(gc, s):
    '''
    Find last update of first worksheet of a spreadsheet
    Used to filter what sheets to work on (for ex "last week updates only, etc.")
    XXX TODO
    '''
    last_update = s.sheet1.updated
    return True

def list_find(data, value):
    '''Return position (index) in list of list, of the first @value found.
    The match is case insensitive.
    Returns empty list if nothing is found.
    @data = list(list(), ...)
    @value str'''
    value = value.lower()

    for x, cells in enumerate(data):
        try:
            cells_lower = [item.lower().strip().lstrip().replace('\n', ' ') for item in cells]
            y = cells_lower.index(value)
        except ValueError:
            continue
        yield x, y

def cell_value_near(s, value, xmoves=1, ymoves=0):
    '''
    Returns value of cell near the first cell found containing 'value'.
    'Near' is defined as by the (x,y) coordinates, default to "right of the value found" ie x=+1, y=0
    x=0 y=+1 would mean "under the value found".

    Ex:
       A      | B
    1| Name   | Bob
    2| Client | Jim

    cell_value_rightof('Name') will return 'Bob'

    Function returns empty string if nothing is found.

    @s: worksheet list data (s=[row][col]) from gspread.model.Worksheet.get_all_values()
    @value: string
    @xmoves, ymoves: number of right lateral moves to find the field value to return
    '''

    res = [match for match in list_find(s, value)][0]

    # Nothing found
    if len(res) == 0:
        return ''

    try:
        return s[res[0]+ymoves][res[1]+xmoves].strip('\n')
    except IndexError:
        return ''

def validate_entry(value, allowed):
    '''
    Check input value against a list of allowed data
    Return value or 'Unknown'.
    @allowed: list()
    @value: str
    '''
    if value in allowed:
        return value.strip('\n')
    return 'Unknown'

def parse_rra_100(gc, sheet, name, version, rrajson, data_levels, risk_levels):
    '''
    called by parse_rra virtual function wrapper
    @gc google gspread connection
    @sheet spreadsheet
    @name spreadsheet name
    @version RRA version detected
    @rrajson writable template for the JSON format of the RRA
    @data_levels list of data levels allowed
    @risk_levels list of risk levels allowed
    '''
    s = sheet.sheet1
    ws = sheet.worksheet('Questions work sheet')

    #Fetch/export all data for faster processing
    #Format is sheet_data[row][col] with positions starting at 0, i.e.:
    #cell(1,2) is sheet_data[0,1]
    sheet_data = s.get_all_values()
    wsheet_data = ws.get_all_values()

    rrajson.source = sheet.id
    metadata = rrajson.details.metadata
    metadata.service = cell_value_near(sheet_data, 'Project Name')
    metadata.scope = cell_value_near(sheet_data, 'Scope')
    try:
        metadata.owner = cell_value_near(sheet_data, 'Project, Data owner') + ' ' + cell_value_near(sheet_data, 'Project, Data owner', xmoves=2)
    except IndexError:
        #<100 format, really
        metadata.owner = cell_value_near(sheet_data, 'Owner') + ' ' + cell_value_near(sheet_data, 'Owner', xmoves=2)

    metadata.developer = cell_value_near(sheet_data, 'Developer') + ' ' + cell_value_near(sheet_data, 'Developer', xmoves=2)
    metadata.operator = cell_value_near(sheet_data, 'Operator') + ' ' + cell_value_near(sheet_data, 'Operator', xmoves=2)

    rrajson.summary = 'RRA for {}'.format(metadata.service)
    rrajson.timestamp = toUTC(datetime.now()).isoformat()
    rrajson.lastmodified = toUTC(s.updated).isoformat()

    data = rrajson.details.data
    data.default = 'Unknown'

    C = rrajson.details.risk.confidentiality
    I = rrajson.details.risk.integrity
    A = rrajson.details.risk.availability

    C.reputation.impact = validate_entry(cell_value_near(sheet_data, 'Confidentiality'), risk_levels)
    C.reputation.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=1)
    C.finances.impact = validate_entry(cell_value_near(sheet_data, 'Confidentiality', xmoves=2), risk_levels)
    C.finances.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=7)
    C.productivity.impact = validate_entry(cell_value_near(sheet_data, 'Confidentiality', xmoves=3), risk_levels)
    C.productivity.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=13)
    # RRA v1.0.0 uses Recovery + Access Control to represent integrity.
    # Access Control is closest to real integrity, so we use that.
    I.reputation.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=3)+','+cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=4)
    I.finances.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=9)+','+cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=10)
    I.productivity.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0,ymoves=15)+','+cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=16)
    I.reputation.impact = validate_entry(cell_value_near(sheet_data, 'Access Control'), risk_levels)
    I.finances.impact = validate_entry(cell_value_near(sheet_data, 'Access Control', xmoves=2), risk_levels)
    I.productivity.impact = validate_entry(cell_value_near(sheet_data, 'Access Control', xmoves=3), risk_levels)
    A.reputation.impact = validate_entry(cell_value_near(sheet_data, 'Availability'), risk_levels)
    A.reputation.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=2)
    A.finances.impact = validate_entry(cell_value_near(sheet_data, 'Availability', xmoves=2), risk_levels)
    A.finances.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=8)
    A.productivity.impact = validate_entry(cell_value_near(sheet_data, 'Availability', xmoves=3), risk_levels)
    A.productivity.rationale = cell_value_near(wsheet_data, 'RATIONALE', xmoves=0, ymoves=14)

    return rrajson

def parse_rra_230(gc, sheet, name, version, rrajson, data_levels, risk_levels):
    '''
    called by parse_rra virtual function wrapper
    @gc google gspread connection
    @sheet spreadsheet
    @name spreadsheet name
    @version RRA version detected
    @rrajson writable template for the JSON format of the RRA
    @data_levels list of data levels allowed
    @risk_levels list of risk levels allowed
    '''

    s = sheet.sheet1
    #Fetch/export all data for faster processing
    #Format is sheet_data[row][col] with positions starting at 0, i.e.:
    #cell(1,2) is sheet_data[0,1]
    sheet_data = s.get_all_values()

    rrajson.source = sheet.id
    metadata = rrajson.details.metadata
    metadata.service = cell_value_near(sheet_data, 'Service name')
    metadata.scope = cell_value_near(sheet_data, 'RRA Scope')
    metadata.owner = cell_value_near(sheet_data, 'Service owner')
    metadata.developer = cell_value_near(sheet_data, 'Developer')
    metadata.operator = cell_value_near(sheet_data, 'Operator')

    rrajson.summary = 'RRA for {}'.format(metadata.service)
    rrajson.timestamp = toUTC(datetime.now()).isoformat()
    rrajson.lastmodified = toUTC(s.updated).isoformat()

    data = rrajson.details.data
    try:
        data.default = cell_value_near(sheet_data, 'Data classification', xmoves=2)
    except IndexError:
        data.default = cell_value_near(sheet_data, 'Data classification of primary service', xmoves=2)

    #Find/list all data dictionnary
    i = 0
    try:
        res = [match for match in list_find(sheet_data, 'Classification')][0]
    except IndexError:
        #No data dictionary then!
        i=-1
    else:
        if len(res) == 0:
            i = -1

    # if there are more than 100 datatypes, well, that's too many anyway.
    # the 100 limit is a safeguard in case the loop goes wrong due to unexpected data in the sheet
    while ((i != -1) and (i<100)):
        i = i+1
        data_level = sheet_data[res[0]+i][res[1]]
        data_type = sheet_data[res[0]+i][res[1]-2]
        if data_level == '':
            #Bail out - list ended/data not found/list broken/etc.
            i = -1
            continue

        for d in data_levels:
            if data_level == d:
                data[d].append(data_type)

    C = rrajson.details.risk.confidentiality
    I = rrajson.details.risk.integrity
    A = rrajson.details.risk.availability

    impact = 'Impact Level'
    try:
        C.reputation.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=1), risk_levels)
    except IndexError:
        impact = 'Impact to Mozilla'
        C.reputation.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=1), risk_levels)
    C.reputation.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=1)
    C.finances.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=2), risk_levels)
    C.finances.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=7)
    C.productivity.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=3), risk_levels)
    C.productivity.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=4)
    I.reputation.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=4), risk_levels)
    I.reputation.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=3)
    I.finances.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=5), risk_levels)
    I.finances.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=9)
    I.productivity.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=6), risk_levels)
    I.productivity.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=6)
    A.reputation.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=7), risk_levels)
    A.reputation.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=2)
    A.finances.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=8), risk_levels)
    A.finances.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=8)
    A.productivity.impact = validate_entry(cell_value_near(sheet_data, impact, xmoves=0, ymoves=9), risk_levels)
    A.productivity.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=5)

    return rrajson

def parse_rra_240(gc, sheet, name, version, rrajson, data_levels, risk_levels):
    '''
    240 and 241 are about the same
    '''
    return parse_rra_241(gc, sheet, name, version, rrajson, data_levels, risk_levels)

def parse_rra_241(gc, sheet, name, version, rrajson, data_levels, risk_levels):
    '''
    called by parse_rra virtual function wrapper
    @gc google gspread connection
    @sheet spreadsheet
    @name spreadsheet name
    @version RRA version detected
    @rrajson writable template for the JSON format of the RRA
    @data_levels list of data levels allowed
    @risk_levels list of risk levels allowed
    '''

    s = sheet.sheet1
    #Fetch/export all data for faster processing
    #Format is sheet_data[row][col] with positions starting at 0, i.e.:
    #cell(1,2) is sheet_data[0,1]
    sheet_data = s.get_all_values()

    rrajson.source = sheet.id
    metadata = rrajson.details.metadata
    metadata.service = cell_value_near(sheet_data, 'Service name')
    metadata.scope = cell_value_near(sheet_data, 'RRA Scope')
    metadata.owner = cell_value_near(sheet_data, 'Service owner')
    metadata.developer = cell_value_near(sheet_data, 'Developer')
    metadata.operator = cell_value_near(sheet_data, 'Operator')

    rrajson.summary = 'RRA for {}'.format(metadata.service)
    rrajson.timestamp = toUTC(datetime.now()).isoformat()
    rrajson.lastmodified = toUTC(s.updated).isoformat()

    data = rrajson.details.data
    data.default = cell_value_near(sheet_data, 'Service Data classification', xmoves=2)

    #Find/list all data dictionnary
    res = [match for match in list_find(sheet_data, 'Data Classification')][0]
    i = 0
    if len(res) == 0:
        i = -1

    # if there are more than 100 datatypes, well, that's too many anyway.
    # the 100 limit is a safeguard in case the loop goes wrong due to unexpected data in the sheet
    while ((i != -1) and (i<100)):
        i = i+1
        data_level = sheet_data[res[0]+i][res[1]]
        data_type = sheet_data[res[0]+i][res[1]-2]
        if data_level == '':
            #Bail out - list ended/data not found/list broken/etc.
            i = -1
            continue

        for d in data_levels:
            if data_level == d:
                data[d].append(data_type)

    C = rrajson.details.risk.confidentiality
    I = rrajson.details.risk.integrity
    A = rrajson.details.risk.availability

    C.reputation.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=1), risk_levels)
    C.finances.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=2), risk_levels)
    C.productivity.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=3), risk_levels)
    I.reputation.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=4), risk_levels)
    I.finances.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=5), risk_levels)
    I.productivity.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=6), risk_levels)
    A.reputation.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=7), risk_levels)
    A.finances.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=8), risk_levels)
    A.productivity.impact = validate_entry(cell_value_near(sheet_data, 'Impact', xmoves=0, ymoves=9), risk_levels)


    C.reputation.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=1)
    C.productivity.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=2)
    C.finances.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=3)
    I.reputation.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=7)
    I.productivity.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=8)
    I.finances.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=9)
    A.reputation.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=4)
    A.productivity.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=5)
    A.finances.rationale = cell_value_near(sheet_data, 'Rationale', xmoves=0, ymoves=6)


    #Depending on the weather this field is called Probability or Likelihood... the format is otherwise identical.
    try:
        probability = 'Probability'
        C.reputation.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=1), risk_levels)
    except IndexError:
        probability = 'Likelihood'
        C.reputation.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=1), risk_levels)

    C.finances.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=2), risk_levels)
    C.productivity.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=3), risk_levels)
    I.reputation.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=4), risk_levels)
    I.finances.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=5), risk_levels)
    I.productivity.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=6), risk_levels)
    A.reputation.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=7), risk_levels)
    A.finances.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=8), risk_levels)
    A.productivity.probability = validate_entry(cell_value_near(sheet_data, probability, xmoves=0, ymoves=9), risk_levels)

    return rrajson

def main():
    with open('rra2json.json') as fd:
        config = json.load(fd)
        authconfig = config['oauth2']
        rrajson_skel = config['rrajson']
        data_levels = config['data_levels']
        risk_levels = config['risk_levels']

    gc = gspread_authorize(authconfig['client_email'], authconfig['private_key'], authconfig['spread_scope'])

    if not gc:
        fatal('Authorization failed')

    # Looking at the XML feed is the only way to get sheet document title for some reason.
    sheets = get_sheet_titles(gc)
    # Do not traverse sheets manually, it's very slow due to the API delays.
    # Opening all at once, including potentially non-useful sheet is a zillion times faster as it's a single API call.
    gsheets = gc.openall()
    for s in gsheets:
        rra_version = detect_version(gc, s)
        if rra_version != None:
            #virtual function pointer
            parse_rra = globals()["parse_rra_{}".format(rra_version)]
            try:
                rrajsondoc = parse_rra(gc, s, sheets[s.id], rra_version, DotDict(dict(rrajson_skel)), list(data_levels),
                        list(risk_levels))
                print(rrajsondoc)
            except:
                import traceback
                traceback.print_exc()
                debug('Exception occured while parsing RRA {} - id {}'.format(sheets[s.id], s.id))

            post_rra_to_mozdef(config['mozdef'], rrajsondoc)

            debug('Parsed {}: {}'.format(sheets[s.id], rra_version))
        else:
            debug('Document {} ({}) could not be parsed and is probably not an RRA'.format(sheets[s.id], s.id))

if __name__ == "__main__":
    main()
