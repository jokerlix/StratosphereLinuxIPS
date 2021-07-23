import redis
import time
import json
from typing import Tuple, Dict, Set, Callable
import configparser
import traceback
from datetime import datetime
import ipaddress

def timing(f):
    """ Function to measure the time another function takes."""
    def wrap(*args):
        time1 = time.time()
        ret = f(*args)
        time2 = time.time()
        print('[DB] Function took {:.3f} ms'.format((time2-time1)*1000.0))
        return ret
    return wrap
class Database(object):
    """ Database object management """
    def __init__(self):
        # The name is used to print in the outputprocess
        self.name = 'DB'
        self.separator = '_'
        self.normal_label = 'normal'
        self.malicious_label = 'malicious'

    def start(self, config):
        """ Start the DB. Allow it to read the conf """
        self.config = config
        # Read values from the configuration file
        try:
            deletePrevdbText = self.config.get('parameters', 'deletePrevdb')
            if deletePrevdbText == 'True':
                self.deletePrevdb = True
            elif deletePrevdbText == 'False':
                self.deletePrevdb = False
        except (configparser.NoOptionError, configparser.NoSectionError, NameError, ValueError, KeyError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.deletePrevdb = True
        try:
            data = self.config.get('parameters', 'time_window_width')
            self.width = float(data)
        except ValueError:
            # Its not a float
            if 'only_one_tw' in data:
                # Only one tw. Width is 10 9s, wich is ~11,500 days, ~311 years
                self.width = 9999999999
        except configparser.NoOptionError:
            # By default we use 3600 seconds, 1hs
            self.width = 3600
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no
            # configuration file specified
            self.width = 3600
        # Create the connection to redis
        if not hasattr(self, 'r'):
            try:
                # db 0 changes everytime we run slips
                self.r = redis.StrictRedis(host='localhost', port=6379, db=0, charset="utf-8", decode_responses=True) #password='password')
                # db 1 is cache, delete it using -cc flag
                self.rcache = redis.StrictRedis(host='localhost', port=6379, db=1, charset="utf-8", decode_responses=True) #password='password')
                if self.deletePrevdb:
                    self.r.flushdb()
            except redis.exceptions.ConnectionError:
                print('[DB] Error in database.py: Is redis database running? You can run it as: "redis-server --daemonize yes"')
        # Even if the DB is not deleted. We need to delete some temp data
        # Zeek_files
        self.r.delete('zeekfiles')
        # By default the slips internal time is 0 until we receive something
        self.setSlipsInternalTime(0)

    def print(self, text, verbose=1, debug=0):
        """
        Function to use to print text using the outputqueue of slips.
        Slips then decides how, when and where to print this text by taking all the prcocesses into account
        Input
         verbose: is the minimum verbosity level required for this text to be printed
         debug: is the minimum debugging level required for this text to be printed
         text: text to print. Can include format like 'Test {}'.format('here')
        If not specified, the minimum verbosity level required is 1, and the minimum debugging level is 0
        """
        vd_text = str(int(verbose) * 10 + int(debug))
        self.outputqueue.put(vd_text + '|' + self.name + '|[' + self.name + '] ' + str(text))

    def setOutputQueue(self, outputqueue):
        """ Set the output queue"""
        self.outputqueue = outputqueue

    def addProfile(self, profileid, starttime, duration):
        """
        Add a new profile to the DB. Both the list of profiles and the hasmap of profile data
        Profiles are stored in two structures. A list of profiles (index) and individual hashmaps for each profile (like a table)
        Duration is only needed for registration purposes in the profile. Nothing operational
        """
        try:
            if not self.r.sismember('profiles', str(profileid)):
                # Add the profile to the index. The index is called 'profiles'
                self.r.sadd('profiles', str(profileid))
                # Create the hashmap with the profileid. The hasmap of each profile is named with the profileid
                # Add the start time of profile
                self.r.hset(profileid, 'starttime', starttime)
                # For now duration of the TW is fixed
                self.r.hset(profileid, 'duration', duration)
                # The IP of the profile should also be added as a new IP we know about.
                ip = profileid.split(self.separator)[1]
                # If the ip is new add it to the list of ips
                self.setNewIP(ip)
                # Publish that we have a new profile
                self.publish('new_profile', ip)
        except redis.exceptions.ResponseError as inst:
            self.outputqueue.put('00|database|Error in addProfile in database.py')
            self.outputqueue.put('00|database|{}'.format(type(inst)))
            self.outputqueue.put('00|database|{}'.format(inst))

    def add_mac_addr_to_profile(self,profileid, mac_addr):
        """ Used when mac adddr  """
        # Add the MAC addr of this profile
        self.r.hset(profileid,'MAC', mac_addr)

    def getProfileIdFromIP(self, daddr_as_obj):
        """ Receive an IP and we want the profileid"""
        try:
            temp_id = 'profile' + self.separator + str(daddr_as_obj)
            data = self.r.sismember('profiles', temp_id)
            if data:
                return temp_id
            return False
        except redis.exceptions.ResponseError as inst:
            self.outputqueue.put('00|database|error in addprofileidfromip in database.py')
            self.outputqueue.put('00|database|{}'.format(type(inst)))
            self.outputqueue.put('00|database|{}'.format(inst))

    def getProfiles(self):
        """ Get a list of all the profiles """
        profiles = self.r.smembers('profiles')
        if profiles != set():
            return profiles
        else:
            return {}

    def getProfileData(self, profileid):
        """ Get all the data for this particular profile.
        Returns:
        A json formated representation of the hashmap with all the data of the profile
        """
        profile = self.r.hgetall(profileid)
        if profile != set():
            return profile
        else:
            return False

    def getTWsfromProfile(self, profileid):
        """
        Receives a profile id and returns the list of all the TW in that profile
        Returns a list with data or an empty list
        """
        data = self.r.zrange('tws' + profileid, 0, -1, withscores=True)
        return data

    def getamountTWsfromProfile(self, profileid):
        """
        Receives a profile id and returns the list of all the TW in that profile
        """
        return len(self.r.zrange('tws' + profileid, 0, -1, withscores=True))

    def getSrcIPsfromProfileTW(self, profileid, twid):
        """
        Get the src ip for a specific TW for a specific profileid
        """
        data = self.r.hget(profileid + self.separator + twid, 'SrcIPs')
        return data

    def getDstIPsfromProfileTW(self, profileid, twid):
        """
        Get the dst ip for a specific TW for a specific profileid
        """
        data = self.r.hget(profileid + self.separator + twid, 'DstIPs')
        return data

    def getT2ForProfileTW(self, profileid, twid, tupleid, tuple_key: str):
        """
        Get T1 and the previous_time for this previous_time, twid and tupleid
        """
        try:
            hash_id = profileid + self.separator + twid
            data = self.r.hget(hash_id, tuple_key)
            if not data:
                return False, False
            data = json.loads(data)
            try:
                (_, previous_two_timestamps) = data[tupleid]
                return previous_two_timestamps
            except KeyError:
                return False, False
        except Exception as e:
            self.outputqueue.put('01|database|[DB] Error in getT2ForProfileTW in database.py')
            self.outputqueue.put('01|database|[DB] {}'.format(type(e)))
            self.outputqueue.put('01|database|[DB] {}'.format(e))
            self.outputqueue.put("01|profiler|[Profile] {}".format(traceback.format_exc()))

    def hasProfile(self, profileid):
        """ Check if we have the given profile """
        return self.r.sismember('profiles', profileid)

    def getProfilesLen(self):
        """ Return the amount of profiles. Redis should be faster than python to do this count """
        return self.r.scard('profiles')

    def getLastTWforProfile(self, profileid):
        """ Return the last TW id and the time for the given profile id """
        data = self.r.zrange('tws' + profileid, -1, -1, withscores=True)
        return data

    def getFirstTWforProfile(self, profileid):
        """ Return the first TW id and the time for the given profile id """
        data = self.r.zrange('tws' + profileid, 0, 0, withscores=True)
        return data

    def getTWforScore(self, profileid, time):
        """
        Return the TW id and the time for the TW that includes the given time.
        The score in the DB is the start of the timewindow, so we should search
        a TW that includes the given time by making sure the start of the TW
        is < time, and the end of the TW is > time.
        """
        # [-1] so we bring the last TW that matched this time.
        try:
            data = self.r.zrangebyscore('tws' + profileid, float('-inf'), float(time), withscores=True, start=0, num=-1)[-1]
        except IndexError:
            # We dont have any last tw?
            data = self.r.zrangebyscore('tws' + profileid, 0, float(time), withscores=True, start=0, num=-1)
        return data

    def addNewOlderTW(self, profileid, startoftw):
        try:
            """	
            Creates or adds a new timewindow that is OLDER than the first we have	
            Return the id of the timewindow just created	
            """
            # Get the first twid and obtain the new tw id
            try:
                (firstid, firstid_time) = self.getFirstTWforProfile(profileid)[0]
                # We have a first id
                # Decrement it!!
                twid = 'timewindow' + str(int(firstid.split('timewindow')[1]) - 1)
            except IndexError:
                # Very weird error, since the first TW MUST exist. What are we doing here?
                pass
            # Add the new TW to the index of TW
            data = {}
            data[str(twid)] = float(startoftw)
            self.r.zadd('tws' + profileid, data)
            self.outputqueue.put('04|database|[DB]: Created and added to DB the new older TW with id {}. Time: {} '.format(twid, startoftw))
            # The creation of a TW now does not imply that it was modified. You need to put data to mark is at modified
            return twid
        except redis.exceptions.ResponseError as e:
            self.outputqueue.put('01|database|error in addNewOlderTW in database.py')
            self.outputqueue.put('01|database|{}'.format(type(e)))
            self.outputqueue.put('01|database|{}'.format(e))

    def addNewTW(self, profileid, startoftw):
        try:
            """ 	
            Creates or adds a new timewindow to the list of tw for the given profile	
            Add the twid to the ordered set of a given profile 	
            Return the id of the timewindow just created	
            We should not mark the TW as modified here, since there is still no data on it, and it may remain without data.	
            """
            # Get the last twid and obtain the new tw id
            try:
                (lastid, lastid_time) = self.getLastTWforProfile(profileid)[0]
                # We have a last id
                # Increment it
                twid = 'timewindow' + str(int(lastid.split('timewindow')[1]) + 1)
            except IndexError:
                # There is no first TW, create it
                twid = 'timewindow1'
            # Add the new TW to the index of TW
            data = {}
            data[str(twid)] = float(startoftw)
            self.r.zadd('tws' + profileid, data)
            self.outputqueue.put('04|database|[DB]: Created and added to DB for profile {} on TW with id {}. Time: {} '.format(profileid, twid, startoftw))
            # The creation of a TW now does not imply that it was modified. You need to put data to mark is at modified
            return twid
        except redis.exceptions.ResponseError as e:
            self.outputqueue.put('01|database|Error in addNewTW')
            self.outputqueue.put('01|database|{}'.format(e))

    def getTimeTW(self, profileid, twid):
        """ Return the time when this TW in this profile was created """
        # Get all the TW for this profile
        # We need to encode it to 'search' because the data in the sorted set is encoded
        data = self.r.zscore('tws' + profileid, twid.encode('utf-8'))
        return data

    def getAmountTW(self, profileid):
        """ Return the amount of tw for this profile id """
        return self.r.zcard('tws' + profileid)

    def getModifiedTWSinceTime(self, time):
        """ Return all the list of modified tw since a certain time"""
        data = self.r.zrangebyscore('ModifiedTW', time, float('+inf'), withscores=True)
        if not data:
            return []
        return data

    def getModifiedTW(self):
        """ Return all the list of modified tw """
        data = self.r.zrange('ModifiedTW', 0, -1, withscores=True)
        if not data:
            return []
        return data

    def wasProfileTWModified(self, profileid, twid):
        """ Retrieve from the db if this TW of this profile was modified """
        data = self.r.zrank('ModifiedTW', profileid + self.separator + twid)
        if not data:
            # If for some reason we don't have the modified bit set,
            # then it was not modified.
            return False
        return True

    def getModifiedTWTime(self, profileid, twid):
        """
        Get the time when this TW was modified
        """
        data = self.r.zcore('ModifiedTW', profileid + self.separator + twid)
        if not data:
            data = -1
        return data

    def getSlipsInternalTime(self):
        return self.r.get('slips_internal_time')

    def setSlipsInternalTime(self, timestamp):
        self.r.set('slips_internal_time', timestamp)

    def markProfileTWAsClosed(self, profileid_tw):
        """
        Mark the TW as closed so tools can work on its data
        """
        self.r.sadd('ClosedTW', profileid_tw)
        self.r.zrem('ModifiedTW', profileid_tw)
        self.publish('tw_closed', profileid_tw)

    def markProfileTWAsModified(self, profileid, twid, timestamp):
        """
        Mark a TW in a profile as modified
        This means:
        1- To add it to the list of ModifiedTW
        2- Add the timestamp received to the time_of_last_modification
           in the TW itself
        3- To update the internal time of slips
        4- To check if we should 'close' some TW
        """
        # Add this tw to the list of modified TW, so others can
        # check only these later
        data = {}
        timestamp = time.time()
        data[profileid + self.separator + twid] = float(timestamp)
        self.r.zadd('ModifiedTW', data)
        self.publish('tw_modified', profileid + ':' + twid)
        # Check if we should close some TW
        self.check_TW_to_close()

    def check_TW_to_close(self):
        """
        Check if we should close some TW
        Search in the modifed tw list and compare when they
        were modified with the slips internal time
        """
        # Get internal time
        sit = self.getSlipsInternalTime()
        # for each modified profile
        modification_time = float(sit) - self.width
        # To test the time
        modification_time = float(sit) - 20
        profiles_tws_to_close = self.r.zrangebyscore('ModifiedTW', 0, modification_time, withscores=True)
        for profile_tw_to_close in profiles_tws_to_close:
            profile_tw_to_close_id = profile_tw_to_close[0]
            profile_tw_to_close_time = profile_tw_to_close[1]
            self.print(f'The profile id {profile_tw_to_close_id} has to be closed because it was last modifed on {profile_tw_to_close_time} and we are closing everything older than {modification_time}. Current time {sit}. Difference: {modification_time - profile_tw_to_close_time}', 7, 0)
            self.markProfileTWAsClosed(profile_tw_to_close_id)

    def add_ips(self, profileid, twid, ip_as_obj, columns, role: str):
        """
        Function to add information about the an IP address
        The flow can go out of the IP (we are acting as Client) or into the IP
        (we are acting as Server)
        ip_as_obj: IP to add. It can be a dstIP or srcIP depending on the rol
        role: 'Client' or 'Server'
        This function does two things:
            1- Add the ip to this tw in this profile, counting how many times
            it was contacted, and storing it in the key 'DstIPs' or 'SrcIPs'
            in the hash of the profile
            2- Use the ip as a key to count how many times that IP was
            contacted on each port. We store it like this because its the
               pefect structure to detect vertical port scans later on
            3- Check if this IP has any detection in the threat intelligence
            module. The information is added by the module directly in the DB.
        """
        try:
            # Get the fields
            dport = columns['dport']
            sport = columns['sport']
            totbytes = columns['bytes']
            sbytes = columns['sbytes']
            pkts = columns['pkts']
            spkts = columns['spkts']
            state = columns['state']
            proto = columns['proto'].upper()
            daddr = columns['daddr']
            saddr = columns['saddr']
            starttime = columns['starttime']
            uid = columns['uid']
            # Depending if the traffic is going out or not, we are Client or Server
            # Set the type of ip as Dst if we are a client, or Src if we are a server
            if role == 'Client':
                # We are receving and adding a destination address and a dst port
                type_host_key = 'Dst'
            elif role == 'Server':
                type_host_key = 'Src'
            #############
            # Store the Dst as IP address and notify in the channel
            # We send the obj but when accessed as str, it is automatically
            # converted to str
            self.setNewIP(str(ip_as_obj))
            #############
            # Try to find evidence for this ip, in case we need to report it
            # Ask the threat intelligence modules, using a channel, that we need info about this IP
            # The threat intelligence module will process it and store the info back in IPsInfo
            # Therefore both ips will be checked for each flow
            # Check destination ip
            data_to_send = {
                'ip': str(daddr),
                'profileid' : str(profileid),
                'twid' :  str(twid),
                'proto' : str(proto),
                'ip_state' : 'dstip',
                'uid': uid
            }
            data_to_send = json.dumps(data_to_send)
            self.publish('give_threat_intelligence',data_to_send)
            # Check source ip
            data_to_send = {
                'ip': str(saddr),
                'profileid' : str(profileid),
                'twid' :  str(twid),
                'proto' : str(proto),
                'ip_state' : 'srcip',
                'uid': uid
            }
            data_to_send = json.dumps(data_to_send)
            self.publish('give_threat_intelligence',data_to_send)
            if role == 'Client':
                # The profile corresponds to the src ip that received this flow
                # The dstip is here the one receiving data from your profile
                # So check the dst ip
                pass
            elif role == 'Server':
                # The profile corresponds to the dst ip that received this flow
                # The srcip is here the one sending data to your profile
                # So check the src ip
                pass
            #############
            # 1- Count the dstips, and store the dstip in the db of this profile+tw
            self.print('add_ips(): As a {}, add the {} IP {} to profile {}, twid {}'.format(role, type_host_key, str(ip_as_obj), profileid, twid), 0, 5)
            # Get the hash of the timewindow
            hash_id = profileid + self.separator + twid
            # Get the DstIPs data for this tw in this profile
            # The format is data['1.1.1.1'] = 3
            data = self.r.hget(hash_id, type_host_key + 'IPs')
            if not data:
                data = {}
            try:
                # Convert the json str to a dictionary
                data = json.loads(data)
                # Add 1 because we found this ip again
                self.print('add_ips(): Not the first time for this addr. Add 1 to {}'.format(str(ip_as_obj)), 0, 5)
                data[str(ip_as_obj)] += 1
                # Convet the dictionary to json
                data = json.dumps(data)
            except (TypeError, KeyError) as e:
                # There was no previous data stored in the DB
                self.print('add_ips(): First time for addr {}. Count as 1'.format(str(ip_as_obj)), 0, 5)
                data[str(ip_as_obj)] = 1
                # Convet the dictionary to json
                data = json.dumps(data)
            # Store the dstips in the dB
            self.r.hset(hash_id, type_host_key + 'IPs', str(data))
            #############
            # 2- Store, for each ip:
            # - Update how many times each individual DstPort was contacted
            # - Update the total flows sent by this ip
            # - Update the total packets sent by this ip
            # - Update the total bytes sent by this ip
            # Get the state. Established, NotEstablished
            summaryState = __database__.getFinalStateFromFlags(state, pkts)
            # Get the previous data about this key
            prev_data = self.getDataFromProfileTW(profileid, twid, type_host_key, summaryState, proto, role, 'IPs')
            try:
                innerdata = prev_data[str(ip_as_obj)]
                self.print('add_ips(): Adding for dst port {}. PRE Data: {}'.format(dport, innerdata), 0, 3)
                # We had this port
                # We need to add all the data
                innerdata['totalflows'] += 1
                innerdata['totalpkt'] += int(pkts)
                innerdata['totalbytes'] += int(totbytes)
                # Store for each dstip, the dstports
                temp_dstports= innerdata['dstports']
                try:
                    temp_dstports[str(dport)] += int(pkts)
                except KeyError:
                    # First time for this ip in the inner dictionary
                    temp_dstports[str(dport)] = int(pkts)
                innerdata['dstports'] = temp_dstports
                prev_data[str(ip_as_obj)] = innerdata
                self.print('add_ips() Adding for dst port {}. POST Data: {}'.format(dport, innerdata), 0, 3)
            except KeyError:
                # First time for this flow
                innerdata = {}
                innerdata['totalflows'] = 1
                innerdata['totalpkt'] = int(pkts)
                innerdata['totalbytes'] = int(totbytes)
                innerdata['uid'] = uid
                temp_dstports = {}
                temp_dstports[str(dport)] = int(pkts)
                innerdata['dstports'] = temp_dstports
                self.print('add_ips() First time for dst port {}. Data: {}'.format(dport, innerdata), 0, 3)
                prev_data[str(ip_as_obj)] = innerdata
            ###########
            # After processing all the features of the ip, store all the info in the database
            # Convert the dictionary to json
            data = json.dumps(prev_data)
            # Create the key for storing
            key_name = type_host_key + 'IPs' + role + proto.upper() + summaryState
            # Store this data in the profile hash
            self.r.hset(profileid + self.separator + twid, key_name, str(data))
            # Mark the tw as modified
            self.markProfileTWAsModified(profileid, twid, starttime)
        except Exception as inst:
            self.outputqueue.put('01|database|[DB] Error in add_ips in database.py')
            self.outputqueue.put('01|database|[DB] Type inst: {}'.format(type(inst)))
            self.outputqueue.put('01|database|[DB] Inst: {}'.format(inst))

    def refresh_data_tuples(self):
        """
        Go through all the tuples and refresh the data about the ipsinfo
        TODO
        """
        outtuples = self.getOutTuplesfromProfileTW()
        intuples = self.getInTuplesfromProfileTW()

    def add_tuple(self, profileid, twid, tupleid, data_tuple, role, starttime, uid):
        """
        Add the tuple going in or out for this profile
        role: 'Client' or 'Server'
        """
        # If the traffic is going out it is part of our outtuples, if not, part of our intuples
        if role == 'Client':
            tuple_key = 'OutTuples'
        elif role == 'Server':
            tuple_key = 'InTuples'
        try:
            self.print('Add_tuple called with profileid {}, twid {}, tupleid {}, data {}'.format(profileid, twid, tupleid, data_tuple), 0, 5)
            # Get all the InTuples or OutTuples for this profileid in this TW
            hash_id = profileid + self.separator + twid
            data = self.r.hget(hash_id, tuple_key)
            # Separate the symbold to add and the previous data
            (symbol_to_add, previous_two_timestamps) = data_tuple
            if not data:
                # Must be str so we can convert later
                data = '{}'
            # Convert the json str to a dictionary
            data = json.loads(data)
            try:
                stored_tuple = data[tupleid]
                # Disasemble the input
                self.print('Not the first time for tuple {} as an {} for {} in TW {}. Add the symbol: {}. Store previous_times: {}. Prev Data: {}'.format(tupleid, tuple_key, profileid, twid, symbol_to_add, previous_two_timestamps, data), 0, 5)
                # Get the last symbols of letters in the DB
                prev_symbols = data[tupleid][0]
                # Add it to form the string of letters
                new_symbol = prev_symbols + symbol_to_add
                # Bundle the data together
                new_data = (new_symbol, previous_two_timestamps)
                # analyze behavioral model with lstm model if the length is divided by 3 - so we send when there is 3 more characters added
                if len(new_symbol) % 3 == 0:
                    self.publish('new_letters', new_symbol + '-' + profileid + '-' + twid + '-' + str(tupleid) +'-' + uid)
                data[tupleid] = new_data
                self.print('\tLetters so far for tuple {}: {}'.format(tupleid, new_symbol), 0, 6)
                data = json.dumps(data)
            except (TypeError, KeyError) as e:
                # TODO check that this condition is triggered correctly only for the first case and not the rest after...
                # There was no previous data stored in the DB
                self.print('First time for tuple {} as an {} for {} in TW {}'.format(tupleid, tuple_key, profileid, twid), 0, 5)
                # Here get the info from the ipinfo key
                new_data = (symbol_to_add, previous_two_timestamps)
                data[tupleid] = new_data
                # Convet the dictionary to json
                data = json.dumps(data)
            # Store the new data on the db
            self.r.hset(hash_id, tuple_key, str(data))
            # Mark the tw as modified
            self.markProfileTWAsModified(profileid, twid, starttime)
        except Exception as inst:
            self.outputqueue.put('01|database|[DB] Error in add_tuple in database.py')
            self.outputqueue.put('01|database|[DB] Type inst: {}'.format(type(inst)))
            self.outputqueue.put('01|database|[DB] Inst: {}'.format(inst))
            self.outputqueue.put('01|database|[DB] {}'.format(traceback.format_exc()))

    def add_port(self, profileid: str, twid: str, ip_address: str, columns: dict, role: str, port_type: str):
        """
        Store info learned from ports for this flow
        The flow can go out of the IP (we are acting as Client) or into the IP (we are acting as Server)
        role: 'Client' or 'Server'. Client also defines that the flow is going out, Server that is going in
        port_type: 'Dst' or 'Src'. Depending if this port was a destination port or a source port
        """
        try:
            # Extract variables from columns
            dport = columns['dport']
            sport = columns['sport']
            totbytes = columns['bytes']
            sbytes = columns['sbytes']
            pkts = columns['pkts']
            spkts = columns['spkts']
            state = columns['state']
            proto = columns['proto'].upper()
            daddr = columns['daddr']
            saddr = columns['saddr']
            starttime = columns['starttime']
            uid = columns['uid']
            # Choose which port to use based if we were asked Dst or Src
            if port_type == 'Dst':
                port = str(dport)
            elif port_type == 'Src':
                port = str(sport)
            # If we are the Client, we want to store the dstips only
            # If we are the Server, we want to store the srcips only
            # This is the only combination that makes sense.
            if role == 'Client':
                ip_key = 'dstips'
            elif role == 'Server':
                ip_key = 'srcips'
            # Get the state. Established, NotEstablished
            summaryState = __database__.getFinalStateFromFlags(state, pkts)
            # Key
            key_name = port_type + 'Ports' + role + proto + summaryState
            #self.print('add_port(): As a {} storing info about {} port {} for {}. Key: {}.'.format(role, port_type, port, profileid, key_name), 0, 3)
            prev_data = self.getDataFromProfileTW(profileid, twid, port_type, summaryState, proto, role, 'Ports')
            try:
                innerdata = prev_data[port]
                innerdata['totalflows'] += 1
                innerdata['totalpkt'] += int(pkts)
                innerdata['totalbytes'] += int(totbytes)
                temp_dstips = innerdata[ip_key]
                try:
                    temp_dstips[str(ip_address)]['pkts'] += int(pkts)
                except KeyError:
                    temp_dstips[str(ip_address)] = {}
                    temp_dstips[str(ip_address)]['pkts'] = int(pkts)
                    temp_dstips[str(ip_address)]['uid'] = uid
                innerdata[ip_key] = temp_dstips
                prev_data[port] = innerdata
                self.print('add_port(): Adding this new info about port {} for {}. Key: {}. NewData: {}'.format(port, profileid, key_name, innerdata), 0, 3)
            except KeyError:
                # First time for this flow
                innerdata = {}
                innerdata['totalflows'] = 1
                innerdata['totalpkt'] = int(pkts)
                innerdata['totalbytes'] = int(totbytes)
                temp_dstips = {}
                temp_dstips[str(ip_address)] = {}
                temp_dstips[str(ip_address)]['pkts'] = int(pkts)
                temp_dstips[str(ip_address)]['uid'] = uid
                innerdata[ip_key] = temp_dstips
                prev_data[port] = innerdata
                self.print('add_port(): First time for port {} for {}. Key: {}. Data: {}'.format(port, profileid, key_name, innerdata), 0, 3)
            # self.outputqueue.put('01|database|[DB] {} '.format(ip_address))
            # Convet the dictionary to json
            data = json.dumps(prev_data)
            self.print('add_port(): Storing info about port {} for {}. Key: {}. Data: {}'.format(port, profileid, key_name, prev_data), 0, 3)
            # Store this data in the profile hash
            hash_key = profileid + self.separator + twid
            self.r.hset(hash_key, key_name, str(data))
            # Mark the tw as modified
            self.markProfileTWAsModified(profileid, twid, starttime)
        except Exception as inst:
            self.outputqueue.put('01|database|[DB] Error in add_port in database.py')
            self.outputqueue.put('01|database|[DB] Type inst: {}'.format(type(inst)))
            self.outputqueue.put('01|database|[DB] Inst: {}'.format(inst))

    def get_data_from_profile_tw(self, hash_key: str, key_name: str):
        try:
            """	
            key_name = [Src,Dst] + [Port,IP] + [Client,Server] + [TCP,UDP, ICMP, ICMP6] + [Established, NotEstablihed] 	
            Example: key_name = 'SrcPortClientTCPEstablished'	
            """
            data = self.r.hget(hash_key, key_name)
            value = {}
            if data:
                portdata = json.loads(data)
                value = portdata
            return value
        except Exception as inst:
            self.outputqueue.put('01|database|[DB] Error in getDataFromProfileTW in database.py')
            self.outputqueue.put('01|database|[DB] Type inst: {}'.format(type(inst)))
            self.outputqueue.put('01|database|[DB] Inst: {}'.format(inst))

    def getOutTuplesfromProfileTW(self, profileid, twid):
        """ Get the out tuples """
        data = self.r.hget(profileid + self.separator + twid, 'OutTuples')
        return data

    def getInTuplesfromProfileTW(self, profileid, twid):
        """ Get the in tuples """
        data = self.r.hget(profileid + self.separator + twid, 'InTuples')
        return data

    def getFinalStateFromFlags(self, state, pkts):
        """
        Analyze the flags given and return a summary of the state. Should work with Argus and Bro flags
        We receive the pakets to distinguish some Reset connections
        """
        try:
            #self.outputqueue.put('06|database|[DB]: State received {}'.format(state))
            pre = state.split('_')[0]
            try:
                # Try suricata states
                """	
                 There are different states in which a flow can be. 	
                 Suricata distinguishes three flow-states for TCP and two for UDP. For TCP, 	
                 these are: New, Established and Closed,for UDP only new and established.	
                 For each of these states Suricata can employ different timeouts. 	
                 """
                if 'new' in state or 'established' in state:
                    return 'Established'
                elif 'closed' in state:
                    return 'NotEstablished'
                # We have varius type of states depending on the type of flow.
                # For Zeek
                if 'S0' in state or 'REJ' in state or 'RSTOS0' in state or 'RSTRH' in state or 'SH' in state or 'SHR' in state:
                    return 'NotEstablished'
                elif 'S1' in state or 'SF' in state or 'S2' in state or 'S3' in state or 'RSTO' in state or 'RSTP' in state or 'OTH' in state:
                    return 'Established'
                # For Argus
                suf = state.split('_')[1]
                if 'S' in pre and 'A' in pre and 'S' in suf and 'A' in suf:
                    """	
                    Examples:	
                    SA_SA	
                    SR_SA	
                    FSRA_SA	
                    SPA_SPA	
                    SRA_SPA	
                    FSA_FSA	
                    FSA_FSPA	
                    SAEC_SPA	
                    SRPA_SPA	
                    FSPA_SPA	
                    FSRPA_SPA	
                    FSPA_FSPA	
                    FSRA_FSPA	
                    SRAEC_SPA	
                    FSPA_FSRPA	
                    FSAEC_FSPA	
                    FSRPA_FSPA	
                    SRPAEC_SPA	
                    FSPAEC_FSPA	
                    SRPAEC_FSRPA	
                    """
                    return 'Established'
                elif 'PA' in pre and 'PA' in suf:
                    # Tipical flow that was reported in the middle
                    """	
                    Examples:	
                    PA_PA	
                    FPA_FPA	
                    """
                    return 'Established'
                elif 'ECO' in pre:
                    return 'ICMP Echo'
                elif 'ECR' in pre:
                    return 'ICMP Reply'
                elif 'URH' in pre:
                    return 'ICMP Host Unreachable'
                elif 'URP' in pre:
                    return 'ICMP Port Unreachable'
                else:
                    """	
                    Examples:	
                    S_RA	
                    S_R	
                    A_R	
                    S_SA 	
                    SR_SA	
                    FA_FA	
                    SR_RA	
                    SEC_RA	
                    """
                    return 'NotEstablished'
            except IndexError:
                # suf does not exist, which means that this is some ICMP or no response was sent for UDP or TCP
                if 'ECO' in pre:
                    # ICMP
                    return 'Established'
                elif 'UNK' in pre:
                    # ICMP6 unknown upper layer
                    return 'Established'
                elif 'CON' in pre:
                    # UDP
                    return 'Established'
                elif 'INT' in pre:
                    # UDP trying to connect, NOT preciselly not established but also NOT 'Established'. So we considered not established because there
                    # is no confirmation of what happened.
                    return 'NotEstablished'
                elif 'EST' in pre:
                    # TCP
                    return 'Established'
                elif 'RST' in pre:
                    # TCP. When -z B is not used in argus, states are single words. Most connections are reseted when finished and therefore are established
                    # It can happen that is reseted being not established, but we can't tell without -z b.
                    # So we use as heuristic the amount of packets. If <=3, then is not established because the OS retries 3 times.
                    if int(pkts) <= 3:
                        return 'NotEstablished'
                    else:
                        return 'Established'
                elif 'FIN' in pre:
                    # TCP. When -z B is not used in argus, states are single words. Most connections are finished with FIN when finished and therefore are established
                    # It can happen that is finished being not established, but we can't tell without -z b.
                    # So we use as heuristic the amount of packets. If <=3, then is not established because the OS retries 3 times.
                    if int(pkts) <= 3:
                        return 'NotEstablished'
                    else:
                        return 'Established'
                else:
                    """	
                    Examples:	
                    S_	
                    FA_	
                    PA_	
                    FSA_	
                    SEC_	
                    SRPA_	
                    """
                    return 'NotEstablished'
            self.outputqueue.put('01|database|[DB] Funcion getFinalStateFromFlags() We didnt catch the state. We should never be here')
            return None
        except Exception as inst:
            self.outputqueue.put('01|database|[DB] Error in getFinalStateFromFlags() in database.py')
            self.outputqueue.put('01|database|[DB] Type inst: {}'.format(type(inst)))
            self.outputqueue.put('01|database|[DB] Inst: {}'.format(inst))
            self.print(traceback.format_exc())

    def getFieldSeparator(self):
        """ Return the field separator """
        return self.separator

    def setEvidence(self, type_detection, detection_info, type_evidence,
                    threat_level, confidence, description, profileid='', twid='', uid=''):
        """
        Set the evidence for this Profile and Timewindow.
        Parameters:
            key: This is how your evidences are grouped. E.g. if you are detecting horizontal port scans,
                 then this would be the port used. The idea is that you can later update
                 this specific detection when it evolves. Examples of keys are:
                 'dport:1234' for all the evidences regarding this dport,
                 'dip:1.1.1.1' for all the evidences regarding that dst ip
        type_evidence: determine the type of evidenc. E.g. PortScan, ThreatIntelligence
        threat_level: determine the importance of the evidence.
        confidence: determine the confidence of the detection. (How sure you are that this is what you say it is.)
        uid: needed to get the flow from the database
        Example:
        The evidence is stored as a dict.
        {
            'dport:32432:PortScanType1': [confidence, threat_level, 'Super complicated portscan on port 32432'],
            'dip:10.0.0.1:PortScanType2': [confidence, threat_level, 'Horizontal port scan on ip 10.0.0.1']
            'dport:454:Attack3': [confidence, threat_level, 'Buffer Overflow']
        }
        """
        # Check if we have and get the current evidence stored in the DB fot this profileid in this twid
        current_evidence = self.getEvidenceForTW(profileid, twid)
        if current_evidence:
            current_evidence = json.loads(current_evidence)
        else:
            current_evidence = {}
        # Prepare key for a new evidence
        key = dict()
        key['type_detection'] = type_detection
        key['detection_info'] = detection_info
        key['type_evidence'] = type_evidence
        #Prepare data for a new evidence
        data = dict()
        data['confidence']= confidence
        data['threat_level'] = threat_level
        data['description'] = description
        # key uses dictionary format, so it needs to be converted to json to work as a dict key.
        key_json = json.dumps(key)
        # It is done to ignore repetition of the same evidence sent.
        if key_json not in current_evidence.keys():
            evidence_to_send = {
                'profileid': str(profileid),
                'twid': str(twid),
                'key': key,
                'data': data,
                'description': description,
                'uid' : uid
            }
            evidence_to_send = json.dumps(evidence_to_send)
            self.publish('evidence_added', evidence_to_send)

        current_evidence[key_json] = data
        current_evidence_json = json.dumps(current_evidence)
        # Set evidence in the database.
        self.r.hset(profileid + self.separator + twid, 'Evidence', str(current_evidence_json))
        self.r.hset('evidence'+profileid, twid, current_evidence_json)


    def deleteEvidence(self,profileid, twid, key):
        """ Delete evidence from the database """

        current_evidence = self.getEvidenceForTW(profileid, twid)
        if current_evidence:
            current_evidence = json.loads(current_evidence)
        else:
            current_evidence = {}
        key_json = json.dumps(key)
        # Delete the key regardless of whether it is in the dictionary
        current_evidence.pop(key_json, None)
        current_evidence_json = json.dumps(current_evidence)
        self.r.hset(profileid + self.separator + twid, 'Evidence', str(current_evidence_json))
        self.r.hset('evidence'+profileid, twid, current_evidence_json)

    def getEvidenceForTW(self, profileid, twid):
        """ Get the evidence for this TW for this Profile """
        data = self.r.hget(profileid + self.separator + twid, 'Evidence')
        return data

    def checkBlockedProfTW(self, profileid, twid):
        """
        Check if profile and timewindow is blocked
        """
        res = self.r.sismember('BlockedProfTW', profileid + self.separator + twid)
        return res

    def set_first_stage_ensembling_label_to_flow(self, profileid, twid, uid, ensembling_label):
        """
        Add a final label to the flow
        """
        flow = self.get_flow(profileid, twid, uid)
        if flow:
            data = json.loads(flow[uid])
            data['1_ensembling_label'] = ensembling_label
            data = json.dumps(data)
            self.r.hset(profileid + self.separator + twid + self.separator + 'flows', uid, data)

    def set_module_label_to_flow(self, profileid, twid, uid, module_name, module_label):
        """
        Add a module label to the flow
        """
        flow = self.get_flow(profileid, twid, uid)
        if flow:
            data = json.loads(flow[uid])
            # here we dont care if add new module lablel or changing existing one
            data['module_labels'][module_name] = module_label
            data = json.dumps(data)
            self.r.hset(profileid + self.separator + twid + self.separator + 'flows', uid, data)

    def get_module_labels_from_flow(self, profileid, twid, uid):
        """
        Get the label from the flow
        """
        flow = self.get_flow(profileid, twid, uid)
        if flow:
            data = json.loads(flow[uid])
            labels = data['module_labels']
            return labels
        else:
            return {}

    def markProfileTWAsBlocked(self, profileid, twid):
        """ Add this profile and tw to the list of blocked """
        self.r.sadd('BlockedProfTW', profileid + self.separator + twid)

    def getBlockedProfTW(self):
        """ Return all the list of blocked tws """
        data = self.r.smembers('BlockedProfTW')
        return data

    def getDomainData(self, domain):
        """
        Return information about this domain
        Returns a dictionary or False if there is no domain in the database
        We need to separate these three cases:
        1- Domain is in the DB without data. Return empty dict.
        2- Domain is in the DB with data. Return dict.
        3- Domain is not in the DB. Return False
        """
        data = self.rcache.hget('DomainsInfo', domain)
        if data or data == {}:
            # This means the domain was in the database, with or without data
            # Case 1 and 2
            # Convert the data
            data = json.loads(data)
            # print(f'In the DB: Domain {domain}, and data {data}')
        else:
            # The Domain was not in the DB
            # Case 3
            data = False
            # print(f'In the DB: Domain {domain}, and data {data}')
        return data

    def getIPData(self, ip):
        """	
        Return information about this IP	
        Returns a dictionary or False if there is no IP in the database	
        ip: a string
        We need to separate these three cases:	
        1- IP is in the DB without data. Return empty dict.	
        2- IP is in the DB with data. Return dict.	
        3- IP is not in the DB. Return False	
        """
        if type(ip) == ipaddress.IPv4Address or type(ip) == ipaddress.IPv6Address:
            ip = str(ip)
        data = self.rcache.hget('IPsInfo', ip)
        if data:
            # This means the IP was in the database, with or without data
            # Convert the data
            data = json.loads(data)
            # print(f'In the DB: IP {ip}, and data {data}')
        else:
            # The IP was not in the DB
            data = False
            # print(f'In the DB: IP {ip}, and data {data}')
        return data

    def getallIPs(self):
        """ Return list of all IPs in the DB """
        data = self.rcache.hgetall('IPsInfo')
        # data = json.loads(data)
        return data

    def setNewDomain(self, domain: str):
        """
        1- Stores this new domain in the Domains hash
        2- Publishes in the channels that there is a new domain, and that we want
            data from the Threat Intelligence modules
        """
        data = self.getDomainData(domain)
        if data is False:
            # If there is no data about this domain
            # Set this domain for the first time in the IPsInfo
            # Its VERY important that the data of the first time we see a domain
            # must be '{}', an empty dictionary! if not the logic breaks.
            # We use the empty dictionary to find if a domain exists or not
            self.rcache.hset('DomainsInfo', domain, '{}')
            # Publish that there is a new IP ready in the channel
            self.publish('new_dns', domain)

    def setNewIP(self, ip: str):
        """
        1- Stores this new IP in the IPs hash
        2- Publishes in the channels that there is a new IP, and that we want
            data from the Threat Intelligence modules
        Sometimes it can happend that the ip comes as an IP object, but when
        accessed as str, it is automatically
        converted to str
        """
        data = self.getIPData(ip)
        if data is False:
            # If there is no data about this IP
            # Set this IP for the first time in the IPsInfo
            # Its VERY important that the data of the first time we see an IP
            # must be '{}', an empty dictionary! if not the logic breaks.
            # We use the empty dictionary to find if an IP exists or not
            self.rcache.hset('IPsInfo', ip, '{}')
            # Publish that there is a new IP ready in the channel
            self.publish('new_ip', ip)

    def getIP(self, ip):
        """ Check if this ip is the hash of the profiles! """
        data = self.rcache.hget('IPsInfo', ip)
        if data:
            return True
        else:
            return False

    def setInfoForDomains(self, domain: str, domaindata: dict):
        """
        Store information for this domain
        We receive a dictionary, such as {'geocountry': 'rumania'} that we are
        going to store for this domain
        If it was not there before we store it. If it was there before, we
        overwrite it
        """
        # Get the previous info already stored
        data = self.getDomainData(domain)
        if not data:
            # This domain is not in the dictionary, add it first:
            self.setNewDomain(domain)
            # Now get the data, which should be empty, but just in case
            data = self.getDomainData(domain)
        for key in iter(domaindata):
            # domaindata can be {'VirusTotal': [1,2,3,4], 'Malicious': ""}
            # domaindata can be {'VirusTotal': [1,2,3,4]}
            # I think we dont need this anymore of the conversion
            if type(data) == str:
                # Convert the str to a dict
                data = json.loads(data)
            data_to_store = domaindata[key]
            # If there is data previously stored, check if we have
            # this key already
            try:
                # If the key is already stored, do not modify it
                # Check if this decision is ok! or we should modify
                # the data
                _ = data[key]
            except KeyError:
                # There is no data for they key so far. Add it
                data[key] = data_to_store
                newdata_str = json.dumps(data)
                self.rcache.hset('DomainsInfo', domain, newdata_str)
                # Publish the changes
                self.r.publish('dns_info_change', domain)

    def setInfoForIPs(self, ip: str, ipdata: dict):
        """
        Store information for this IP
        We receive a dictionary, such as {'geocountry': 'rumania'} that we are
        going to store for this IP.
        If it was not there before we store it. If it was there before, we
        overwrite it
        """
        # Get the previous info already stored
        data = self.getIPData(ip)
        if data is False:
            # This IP is not in the dictionary, add it first:
            self.setNewIP(ip)
            # Now get the data, which should be empty, but just in case
            data = self.getIPData(ip)

        for key in iter(ipdata):
            data_to_store = ipdata[key]
            # If there is data previously stored, check if we have this key already
            try:
                # We modify value in any case, because there might be new info
                _ = data[key]
            except KeyError:
                # There is no data for they key so far.
                # Publish the changes
                self.r.publish('ip_info_change', ip)
            data[key] = data_to_store
            newdata_str = json.dumps(data)
            self.rcache.hset('IPsInfo', ip, newdata_str)

    def subscribe(self, channel):
        """ Subscribe to channel """
        # For when a TW is modified
        pubsub = self.r.pubsub()
        supported_channels = ['tw_modified' , 'evidence_added' , 'new_ip' ,  'new_flow' , 'new_dns', 'new_dns_flow','new_http', 'new_ssl' , 'new_profile',\
                    'give_threat_intelligence', 'new_letters', 'ip_info_change', 'dns_info_change', 'dns_info_change', 'tw_closed', 'core_messages',\
                    'new_blocking', 'new_ssh','new_notice', 'finished_modules']
        for supported_channel in supported_channels:
            if supported_channel in channel:
                pubsub.subscribe(channel)
                break
        return pubsub

    def publish(self, channel, data):
        """ Publish something """
        self.r.publish(channel, data)

    def publish_stop(self):
        """ Publish stop command to terminate slips """
        all_channels_list = self.r.pubsub_channels()
        self.print('Sending the stop signal to all listeners', 3, 3)
        for channel in all_channels_list:
            self.r.publish(channel, 'stop_process')

    def get_all_flows_in_profileid_twid(self, profileid, twid):
        """
        Return a list of all the flows in this profileid and twid
        """
        data = self.r.hgetall(profileid + self.separator + twid + self.separator + 'flows')
        if data:
            return data

    def get_all_flows(self):
        """
        Returns a list with all the flows in all profileids and twids
        Each position in the list is a dictionary of flows.
        """
        data = []
        for profileid in self.getProfiles():
            for (twid, time) in self.getTWsfromProfile(profileid):
                temp = self.get_all_flows_in_profileid_twid(profileid, twid)
                if temp:
                    data.append(temp)
        return data

    def get_flow(self, profileid, twid, uid):
        """
        Returns the flow in the specific time
        The format is a dictionary
        """
        data = {}
        temp = self.r.hget(profileid + self.separator + twid + self.separator + 'flows', uid)
        data[uid] = temp
        # Get the dictionary format
        return data

    def get_labels(self):
        """ Return the amount of each label so far """
        return self.r.zrange('labels', 0, -1, withscores=True)

    def add_flow(self, profileid='', twid='', stime='', dur='', saddr='', sport='', daddr='', dport='', proto='', state='', pkts='', allbytes='', spkts='', sbytes='', appproto='', uid='', label=''):
        """
        Function to add a flow by interpreting the data. The flow is added to the correct TW for this profile.
        The profileid is the main profile that this flow is related too.
        """
        data = {}
        # data['uid'] = uid
        data['ts'] = stime
        data['dur'] = dur
        data['saddr'] = saddr
        data['sport'] = sport
        data['daddr'] = daddr
        data['dport'] = dport
        data['proto'] = proto
        # Store the interpreted state, not the raw one
        summaryState = __database__.getFinalStateFromFlags(state, pkts)
        data['origstate'] = state
        data['state'] = summaryState
        data['pkts'] = pkts
        data['allbytes'] = allbytes
        data['spkts'] = spkts
        data['sbytes'] = sbytes
        data['appproto'] = appproto
        data['label'] = label
        # when adding a flow, there are still no labels ftom other modules, so the values is empty dictionary
        data['module_labels'] = {}
        # Convert to json string
        data = json.dumps(data)
        # Store in the hash 10.0.0.1_timewindow1, a key uid, with data
        value = self.r.hset(profileid + self.separator + twid + self.separator + 'flows', uid, data)
        if value:
            # The key was not there before. So this flow is not repeated
            # Store the label in our uniq set, and increment it by 1
            if label:
                self.r.zincrby('labels', 1, label)
            # We can publish the flow directly without asking for it, but its good to maintain the format given by the get_flow() function.
            flow = self.get_flow(profileid, twid, uid)
            # Get the dictionary and convert to json string
            flow = json.dumps(flow)
            # Prepare the data to publish.
            to_send = {}
            to_send['profileid'] = profileid
            to_send['twid'] = twid
            to_send['flow'] = flow
            to_send['stime'] = stime
            to_send = json.dumps(to_send)
            self.publish('new_flow', to_send)

    def add_out_ssl(self, profileid, twid, daddr_as_obj, dport, flowtype, uid,
                    version, cipher, resumed, established, cert_chain_fuids,
                    client_cert_chain_fuids, subject, issuer, validation_status, curve, server_name):
        """
        Store in the DB an ssl request
        All the type of flows that are not netflows are stored in a separate hash ordered by uid.
        The idea is that from the uid of a netflow, you can access which other type of info is related to that uid
        """
        data = {}
        data['uid'] = uid
        data['type'] = flowtype
        data['version'] = version
        data['cipher'] = cipher
        data['resumed'] = resumed
        data['established'] = established
        data['cert_chain_fuids'] = cert_chain_fuids
        data['client_cert_chain_fuids'] = client_cert_chain_fuids
        data['subject'] = subject
        data['issuer'] = issuer
        data['validation_status'] = validation_status
        data['curve'] = curve
        data['server_name'] = server_name
        data['daddr'] = str(daddr_as_obj)
        data['dport'] = dport
        # Convert to json string
        data = json.dumps(data)
        self.r.hset(profileid + self.separator + twid + self.separator + 'altflows', uid, data)
        to_send = {}
        to_send['profileid'] = profileid
        to_send['twid'] = twid
        to_send['flow'] = data
        to_send = json.dumps(to_send)
        self.publish('new_ssl', to_send)
        self.print('Adding SSL flow to DB: {}'.format(data), 5, 0)
        # Check if the server_name (SNI) is detected by the threat intelligence. Empty field in the end, cause we have extrafield for the IP.
        # If server_name is not empty, set in the IPsInfo and send to TI
        if server_name:
            # Save new server name in the IPInfo. There might be several server_name per IP.
            ipdata = self.getIPData(str(daddr_as_obj))
            if ipdata:
                sni_ipdata = ipdata.get('SNI', [])
            else:
                sni_ipdata = []

            SNI_port = {'server_name':server_name, 'dport':dport}
            # We do not want any duplicates.
            if SNI_port not in sni_ipdata:
                sni_ipdata.append(SNI_port)
            self.setInfoForIPs(str(daddr_as_obj), {'SNI':sni_ipdata})

            # We are giving only new server_name to the threat_intelligence module.
            data_to_send = {
                'server_name' : server_name,
                'profileid' : str(profileid),
                'twid': str(twid),
                'uid':uid
            }
            data_to_send = json.dumps(data_to_send)
            self.publish('give_threat_intelligence',data_to_send)

    def add_out_http(self, profileid, twid, flowtype, uid, method, host, uri, version, user_agent, request_body_len, response_body_len, status_code, status_msg, resp_mime_types, resp_fuids):
        """
        Store in the DB a http request
        All the type of flows that are not netflows are stored in a separate hash ordered by uid.
        The idea is that from the uid of a netflow, you can access which other type of info is related to that uid
        """
        data = {}
        data['uid'] = uid
        data['type'] = flowtype
        data['method'] = method
        data['host'] = host
        data['uri'] = uri
        data['version'] = version
        data['user_agent'] = user_agent
        data['request_body_len'] = request_body_len
        data['response_body_len'] = response_body_len
        data['status_code'] = status_code
        data['status_msg'] = status_msg
        data['resp_mime_types'] = resp_mime_types
        data['resp_fuids'] = resp_fuids
        # Convert to json string
        data = json.dumps(data)
        self.r.hset(profileid + self.separator + twid + self.separator + 'altflows', uid, data)
        to_send = {}
        to_send['profileid'] = profileid
        to_send['twid'] = twid
        to_send['flow'] = data
        to_send = json.dumps(to_send)
        self.publish('new_http', to_send)
        self.print('Adding HTTP flow to DB: {}'.format(data), 5, 0)
        # Check if the host domain is detected by the threat intelligence. Empty field in the end, cause we have extrafield for the IP.
        data_to_send = {
                'host': host,
                'profileid' : str(profileid),
                'twid' :  str(twid),
                'uid':uid
            }
        data_to_send = json.dumps(data_to_send)
        self.publish('give_threat_intelligence',data_to_send)

    def add_out_ssh(self, profileid, twid, flowtype, uid, ssh_version, auth_attempts, auth_success, client, server, cipher_alg, mac_alg, compression_alg, kex_alg, host_key_alg, host_key):
        """
        Store in the DB a SSH request
        All the type of flows that are not netflows are stored in a
        separate hash ordered by uid.
        The idea is that from the uid of a netflow, you can access which
        other type of info is related to that uid
        """
        #  {"client":"SSH-2.0-OpenSSH_8.1","server":"SSH-2.0-OpenSSH_7.5p1 Debian-5","cipher_alg":"chacha20-pol y1305@openssh.com","mac_alg":"umac-64-etm@openssh.com","compression_alg":"zlib@openssh.com","kex_alg":"curve25519-sha256","host_key_alg":"ecdsa-sha2-nistp256","host_key":"de:04:98:42:1e:2a:06:86:5b:f0:5b:e3:65:9f:9d:aa"}
        data = {}
        data['uid'] = uid
        data['type'] = flowtype
        data['version'] = ssh_version
        data['auth_attempts'] = auth_attempts
        data['auth_success'] = auth_success
        data['client'] = client
        data['server'] = server
        data['cipher_alg'] = cipher_alg
        data['mac_alg'] = mac_alg
        data['compression_alg'] = compression_alg
        data['kex_alg'] = kex_alg
        data['host_key_alg'] = host_key_alg
        data['host_key'] = host_key
        # Convert to json string
        data = json.dumps(data)
        # Set the dns as alternative flow
        self.r.hset(profileid + self.separator + twid + self.separator + 'altflows', uid, data)
        # Publish the new dns received
        to_send = {}
        to_send['profileid'] = profileid
        to_send['twid'] = twid
        to_send['flow'] = data
        to_send['uid'] = uid
        to_send = json.dumps(to_send)
        # publish a dns with its flow
        self.publish('new_ssh', to_send)
        self.print('Adding SSH flow to DB: {}'.format(data), 5, 0)
        # Check if the dns is detected by the threat intelligence. Empty field in the end, cause we have extrafield for the IP.

    def add_out_notice(self,profileid, twid, daddr, sport, dport, note, msg, scanned_port, scanning_ip, uid):
        """" Send notice.log data to new_notice channel to look for self-signed certificates """
        data = {
            'daddr' :  daddr,
            'sport' :  sport,
            'dport' :  dport,
            'note'  :  note,
            'msg'   :  msg,
            'scanned_port' : scanned_port,
            'scanning_ip'  : scanning_ip
        }
        data = json.dumps(data) # this is going to be sent insidethe to_send dict
        to_send = {}
        to_send['profileid'] = profileid
        to_send['twid'] = twid
        to_send['flow'] = data
        to_send['uid'] = uid
        to_send = json.dumps(to_send)
        self.publish('new_notice', to_send)
        self.print('Adding notice flow to DB: {}'.format(data), 5, 0)

    def add_out_dns(self, profileid, twid, flowtype, uid, query, qclass_name, qtype_name, rcode_name, answers, ttls):
        """
        Store in the DB a DNS request
        All the type of flows that are not netflows are stored in a separate hash ordered by uid.
        The idea is that from the uid of a netflow, you can access which other type of info is related to that uid
        """
        data = {}
        data['uid'] = uid
        data['type'] = flowtype
        data['query'] = query
        data['qclass_name'] = qclass_name
        data['qtype_name'] = qtype_name
        data['rcode_name'] = rcode_name
        data['answers'] = answers
        data['ttls'] = ttls
        # Convert to json string
        data = json.dumps(data)
        # Set the dns as alternative flow
        self.r.hset(profileid + self.separator + twid + self.separator + 'altflows', uid, data)
        # Publish the new dns received
        to_send = {}
        to_send['profileid'] = profileid
        to_send['twid'] = twid
        to_send['flow'] = data
        to_send = json.dumps(to_send)
        #publish a dns with its flow
        self.publish('new_dns_flow', to_send)
        self.print('Adding DNS flow to DB: {}'.format(data), 5,0)
        # Check if the dns is detected by the threat intelligence. Empty field in the end, cause we have extrafield for the IP.
        data_to_send = {
                'query': str(query),
                'profileid' : str(profileid),
                'twid' :  str(twid),
                'uid': uid
            }
        data_to_send = json.dumps(data_to_send)
        self.publish('give_threat_intelligence',data_to_send)

    def get_altflow_from_uid(self, profileid, twid, uid):
        """ Given a uid, get the alternative flow realted to it """
        return self.r.hget(profileid + self.separator + twid + self.separator + 'altflows', uid)

    def add_timeline_line(self, profileid, twid, data, timestamp):
        """ Add a line to the time line of this profileid and twid """
        self.print('Adding timeline for {}, {}: {}'.format(profileid, twid, data), 4, 0)
        key = str(profileid + self.separator + twid + self.separator + 'timeline')
        data = json.dumps(data)
        mapping = {}
        mapping[data] = timestamp
        self.r.zadd(key, mapping)
        # Mark the tw as modified since the timeline line is new data in the TW
        self.markProfileTWAsModified(profileid, twid, timestamp='')

    def get_timeline_last_line(self, profileid, twid):
        """ Add a line to the time line of this profileid and twid """
        key = str(profileid + self.separator + twid + self.separator + 'timeline')
        data = self.r.zrange(key, -1, -1)
        return data

    def get_timeline_last_lines(self, profileid, twid, first_index: int) -> Tuple[str, int]:
        """ Get only the new items in the timeline."""
        key = str(profileid + self.separator + twid + self.separator + 'timeline')
        # The the amount of lines in this list
        last_index = self.r.zcard(key)
        # Get the data in the list from the index asked (first_index) until the last
        data = self.r.zrange(key, first_index, last_index - 1)
        return data, last_index

    def get_timeline_all_lines(self, profileid, twid):
        """ Add a line to the time line of this profileid and twid """
        key = str(profileid + self.separator + twid + self.separator + 'timeline')
        data = self.r.zrange(key, 0, -1)
        return data

    def set_port_info(self, portproto, name):
        """ Save in the DB a port with its description """
        self.r.hset('portinfo', portproto, name)

    def get_port_info(self, portproto):
        """ Retrive the name of a port """
        return self.r.hget('portinfo', portproto)

    def add_zeek_file(self, filename):
        """ Add an entry to the list of zeek files """
        self.r.sadd('zeekfiles', filename)

    def get_all_zeek_file(self):
        """ Return all entries from the list of zeek files """
        data = self.r.smembers('zeekfiles')
        return data

    def set_profile_module_label(self, profileid, module, label):
        """
        Set a module label for a profile.
        """
        data = self.get_profile_modules_labels(profileid)
        data[module] = label
        data = json.dumps(data)
        self.r.hset(profileid, 'modules_labels', data)

    def get_profile_modules_labels(self, profileid):
        """
        Get labels set by modules in the profile.
        """
        data = self.r.hget(profileid, 'modules_labels')
        if data:
            data = json.loads(data)
        else:
            data = {}
        return data

    def del_zeek_file(self, filename):
        """ Delete an entry from the list of zeek files """
        self.r.srem('zeekfiles', filename)

    def delete_ips_from_IoC_ips(self, ips):
        """
        Delete old IPs from IoC
        """
        self.rcache.hdel('IoC_ips', *ips)

    def delete_domains_from_IoC_domains(self, domains):
        """
        Delete old domains from IoC
        """
        self.rcache.hdel('IoC_domains', *domains)

    def add_ips_to_IoC(self, ips_and_description: dict) -> None:
        """
        Store a group of IPs in the db as they were obtained from an IoC source
        What is the format of ips_and_description?
        """
        if ips_and_description:
            self.rcache.hmset('IoC_ips', ips_and_description)

    def add_domains_to_IoC(self, domains_and_description: dict) -> None:
        """
        Store a group of domains in the db as they were obtained from
        an IoC source
        What is the format of domains_and_description?
        """
        if domains_and_description:
            self.rcache.hmset('IoC_domains', domains_and_description)

    def add_ip_to_IoC(self, ip: str, description: str) -> None:
        """
        Store in the DB 1 IP we read from an IoC source  with its description
        """
        self.rcache.hset('IoC_ips', ip, description)

    def add_domain_to_IoC(self, domain: str, description: str) -> None:
        """
        Store in the DB 1 domain we read from an IoC source
        with its description
        """
        self.rcache.hset('IoC_domains', domain, description)

    def set_malicious_ip(self, ip, profileid_twid):
        """
        Save in DB malicious IP found in the traffic
        with its profileid and twid
        """
        self.r.hset('MaliciousIPs', ip, profileid_twid)

    def set_malicious_domain(self, domain, profileid_twid):
        """
        Save in DB a malicious domain found in the traffic
        with its profileid and twid
        """
        self.r.hset('MaliciousDomains', domain, profileid_twid)

    def get_malicious_ip(self, ip):
        """
        Return malicious IP and its list of presence in
        the traffic (profileid, twid)
        """
        data = self.r.hget('MaliciousIPs', ip)
        if data:
            data = json.loads(data)
        else:
            data = {}
        return data

    def get_malicious_domain(self, domain):
        """
        Return malicious domain and its list of presence in
        the traffic (profileid, twid)
        """
        data = self.r.hget('MaliciousDomains', domain)
        if data:
            data = json.loads(data)
        else:
            data = {}
        return data

    def set_dns_resolution(self, query, answers):
        """
        Save in DB DNS name for each IP
        """
        for ans in answers:
            data = self.get_dns_resolution(ans)
            if query not in data:
                data.append(query)
            data = json.dumps(data)
            self.r.hset('DNSresolution', ans, data)

    def get_dns_resolution(self, ip):
        """
        Get DNS name of the IP, a list
        """
        data = self.r.hget('DNSresolution', ip)
        if data:
            data = json.loads(data)
            return data
        else:
            return []

    def set_passive_dns(self, ip, data):
        """
        Save in DB passive DNS from virus total
        """
        data = json.dumps(data)
        self.r.hset('passiveDNS', ip, data)

    def get_passive_dns(self, ip):
        """
        Get passive DNS from virus total
        """
        data = self.r.hget('passiveDNS', ip)
        if data:
            data = json.loads(data)
            return data
        else:
            return ''

    def get_IPs_in_IoC(self):
        """
        Get all IPs and their description from IoC_ips
        """
        data = self.rcache.hgetall('IoC_ips')
        return data

    def get_Domains_in_IoC(self):
        """
        Get all Domains and their description from IoC_domains
        """
        data = self.rcache.hgetall('IoC_domains')
        return data

    def search_IP_in_IoC(self, ip: str) -> str:
        """
        Search in the dB of malicious IPs and return a
        description if we found a match
        """
        ip_description = self.rcache.hget('IoC_ips', ip)
        if ip_description == None:
            return False
        else:
            return ip_description

    def get_flow_timestamp(self, profileid, twid, uid):
        """
        Return the timestamp of the flow
        """
        timestamp = ''
        if uid:
            try:
                time.sleep(3) # it takes time for the binetflow to put the flow into the database
                flow_information = self.r.hget(profileid + "_" + twid + "_flows", uid)
                flow_information = json.loads(flow_information)
                timestamp = flow_information.get("ts")
            except:
                pass
        return timestamp

    def search_Domain_in_IoC(self, domain: str) -> str:
        """
        Search in the dB of malicious domainss and return a
        description if we found a match
        """
        domain_description = self.rcache.hget('IoC_domains',domain)
        if domain_description == None:
            # try to match subdomain
            ioc_domains = self.rcache.hgetall('IoC_domains')
            for malicious_domain,description in ioc_domains.items():
                if malicious_domain in domain:
                    return description
            return False
        else:
            return domain_description

    def getDataFromProfileTW(self, profileid: str, twid: str, direction: str, state : str, protocol: str, role: str, type_data: str) -> dict:
        """
        Get the info about a certain role (Client or Server), for a particular protocol (TCP, UDP, ICMP, etc.) for a particular State (Established, etc.)
        direction: 'Dst' or 'Src'. This is used to know if you want the data of the src ip or ports, or the data from the dst ips or ports
        state: can be 'Established' or 'NotEstablished'
        protocol: can be 'TCP', 'UDP', 'ICMP' or 'IPV6ICMP'
        role: can be 'Client' or 'Server'
        type_data: can be 'Ports' or 'IPs'
        """
        try:
            self.print('Asked to get data from profile {}, {}, {}, {}, {}, {}, {}'.format(profileid, twid, direction, state, protocol, role, type_data), 0, 4)
            key = direction + type_data + role + protocol + state
            # self.print('Asked Key: {}'.format(key))
            data = self.r.hget(profileid + self.separator + twid, key)
            value = {}
            if data:
                self.print('Key: {}. Getting info for Profile {} TW {}. Data: {}'.format(key, profileid, twid, data), 5, 0)
                # Convert the dictionary to json
                portdata = json.loads(data)
                value = portdata
            elif not data:
                self.print('There is no data for Key: {}. Profile {} TW {}'.format(key, profileid, twid), 5, 0)
            return value
        except Exception as inst:
            self.outputqueue.put('01|database|[DB] Error in getDataFromProfileTW database.py')
            self.outputqueue.put('01|database|[DB] Type inst: {}'.format(type(inst)))
            self.outputqueue.put('01|database|[DB] Inst: {}'.format(inst))

    def get_last_update_time_malicious_file(self):
        """ Return the time of last update of the remote malicious file from the db """
        return self.r.get('last_update_malicious_file')

    def set_last_update_time_malicious_file(self, time):
        """ Return the time of last update of the remote malicious file from the db """
        self.r.set('last_update_malicious_file', time)

    def get_host_ip(self):
        """ Get the IP addresses of the host from a db. There can be more than one"""
        return self.r.smembers('hostIP')

    def set_host_ip(self, ip):
        """ Store the IP address of the host in a db. There can be more than one"""
        self.r.sadd('hostIP', ip)

    def add_all_loaded_malicous_ips(self, ips_and_description: dict) -> None:
        self.r.hmset('loaded_malicious_ips', ips_and_description)

    def add_loaded_malicious_ip(self, ip: str, description: str) -> None:
        self.r.hset('loaded_malicious_ips', ip, description)

    def get_loaded_malicious_ip(self, ip: str) -> str:
        ip_description = self.r.hget('loaded_malicious_ips', ip)
        return ip_description

    def set_profile_as_malicious(self, profileid: str, description: str) -> None:
        # Add description to this malicious ip profile.
        self.r.hset(profileid, 'labeled_as_malicious', description)

    def is_profile_malicious(self, profileid: str) -> str:
        data = self.r.hget(profileid, 'labeled_as_malicious')
        return data

    def set_malicious_file_info(self, file, data):
        '''
        Set/update time and/or e-tag for malicious file
        '''
        # data = self.get_malicious_file_info(file)
        # for key in file_data:
        # data[key] = file_data[key]
        data = json.dumps(data)
        self.rcache.hset('malicious_files_info', file, data)

    def get_malicious_file_info(self, file):
        '''
        Get malicious file info
        '''
        data = self.rcache.hget('malicious_files_info', file)
        if data:
            data = json.loads(data)
        else:
            data = ''
        return data

    def set_asn_cache(self, asn, asn_range) -> None:
        """
        Stores the range of asn in cached_asn hash
        :param asn: str
        :param asn_range: str
        """
        self.rcache.hset('cached_asn', asn, asn_range)

    def get_asn_cache(self):
        """
        Returns cached asn of ip if present, or False.
        """
        return self.rcache.hgetall('cached_asn')
    
    def store_process_PID(self, process, pid):
        """
        Stores each started process or module with it's PID
        :param pid: int
        :param process: str
        """
        self.r.hset('PIDs', process, pid)

    def get_PIDs(self):
        """ returns a dict with module names as keys and pids as values """
        return self.r.hgetall('PIDs')

    def set_whitelist(self,whitelisted_IPs, whitelisted_domains, whitelisted_organizations):
        """ Store a dict of whitelisted IPs, domains and organizations in the db """

        self.r.hset("whitelist" , "IPs", json.dumps(whitelisted_IPs))
        self.r.hset("whitelist" , "domains", json.dumps(whitelisted_domains))
        self.r.hset("whitelist" , "organizations", json.dumps(whitelisted_organizations))

    def get_whitelist(self):
        """ Return dict of 3 keys: IPs, domains and organizations"""
        return self.r.hgetall('whitelist')


__database__ = Database()
