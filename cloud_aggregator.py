#!/usr/bin/python

"""*
 * Copyright 2009 University of Victoria
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
 * either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * AUTHOR - Adam Bishop - ahbishop@uvic.ca
 *       
 * For comments or questions please contact the above e-mail address 
 * or Ian Gable - igable@uvic.ca
 *
 * """

import ConfigParser
import os
from cStringIO import StringIO
import logging
import sys
from urlparse import urlparse
import urllib2
import gzip
from BaseHTTPServer import BaseHTTPRequestHandler
from redis import Redis, ConnectionError, ResponseError
import time
import xml
import libxml2
import libxslt
import xml.dom.minidom
from xml.dom.pulldom import parseString, START_ELEMENT

RET_CRITICAL = -1

TIME_WINDOW = 600
TIME_STAMP = "TimeStamp"

XSD_SCHEMA = "clouds.xsd"

MONITORING_XML_LOC = "Local_Monitoring_XML_Data"
TARGET_CLOUDS_LOC = "Clouds_Addr_File"
TARGET_XML_PATH = "Target_Monitoring_Data_Path"
TARGET_VM_SLOTS_PATH = "Target_VM_Slots_Path"

TARGET_REDIS_DB = "Target_Redis_DB_Id"
CLOUDS_KEY = "Clouds_Key"
UPDATE_INTERVAL = "Update_Interval"
REDISDB_SERVER_HOSTNAME = "RedisDB_Server_Hostname"
REDISDB_SERVER_PORT = "RedisDB_Server_Port"

CONF_FILE_LOC = "cloud_aggregator.cfg"
CONF_FILE_SECTION = "Cloud_Aggregator"

ConfigMapping = {}


# This global method loads all the user configured options from the configuration file and saves them
# into the ConfigMapping dictionary
def loadConfig(logger):

    cfgFile = ConfigParser.ConfigParser()
    if(os.path.exists(CONF_FILE_LOC)):
        cfgFile.read(CONF_FILE_LOC)
        try:
            ConfigMapping[MONITORING_XML_LOC] = cfgFile.get(CONF_FILE_SECTION,MONITORING_XML_LOC,0)
            ConfigMapping[TARGET_CLOUDS_LOC] = cfgFile.get(CONF_FILE_SECTION,TARGET_CLOUDS_LOC,0)
            ConfigMapping[TARGET_REDIS_DB] = cfgFile.get(CONF_FILE_SECTION,TARGET_REDIS_DB,0)
            ConfigMapping[CLOUDS_KEY] = cfgFile.get(CONF_FILE_SECTION, CLOUDS_KEY,0)            
            ConfigMapping[TARGET_XML_PATH] = cfgFile.get(CONF_FILE_SECTION,TARGET_XML_PATH,0)
            ConfigMapping[TARGET_VM_SLOTS_PATH] = cfgFile.get(CONF_FILE_SECTION,TARGET_VM_SLOTS_PATH,0)           
            ConfigMapping[UPDATE_INTERVAL] = cfgFile.get(CONF_FILE_SECTION, UPDATE_INTERVAL,0)
            ConfigMapping[REDISDB_SERVER_HOSTNAME] = cfgFile.get(CONF_FILE_SECTION, REDISDB_SERVER_HOSTNAME,0)
            ConfigMapping[REDISDB_SERVER_PORT] = cfgFile.get(CONF_FILE_SECTION, REDISDB_SERVER_PORT,0)
   
        except ConfigParser.NoSectionError: 
            logger.error("Unable to locate "+CONF_FILE_SECTION+" section in "+CONF_FILE_LOC+" - Malformed config file?")
            sys.exit(RET_CRITICAL)
        except ConfigParser.NoOptionError, nopt:
            logger.error( nopt.message+" of configuration file")
            sys.exit(RET_CRITICAL)
    else:
        logger.error( "Configuration file not found in this file's directory")
        sys.exit(RET_CRITICAL)

class Loggable:
    """ A simple base class to encapsulate useful logging features - Meant to be derived from

    """
    def __init__(self, callingClass):

        self.logString = StringIO()

        self.logger = logging.getLogger(callingClass)
        self.logger.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s ; %(name)s ; %(levelname)s ; %(message)s')

        errorOutputHndlr = logging.FileHandler("cloud_aggregator.log")
        errorOutputHndlr.setFormatter(formatter)
        errorOutputHndlr.setLevel(logging.DEBUG)

        self.logger.addHandler(errorOutputHndlr)


class CloudAggregatorHTTPRequest(Loggable):
    """
    This class provides very basic HTTP GET functionality. It was created as a helper class for Perceptor to use
    to query remote Clouds. It implements some basic HTTP protocol functionality including gzip support.
    """

    # This can be almost anything, this was chosen arbitrarily. One could masquerade as another browser or
    # agent, but I don't see the need for this currently
    defaultUserAgent = "CloudAggregator/1.0"

    def __init__(self):
        Loggable.__init__(self,self.__class__.__name__)

    # Request the data from the url passed in. This is done with a HTTP GET and the return code is checked to ensure
    # a 200 (sucess) was received
    def request(self, url):

        results = self._req(url)
        if results != None:  
            if results['rStatus'] != 200:
                self.logger.error("Received HTTP Code: "+str(results['rStatus'])+" - "+ BaseHTTPRequestHandler.responses[results['rStatus']][0])      
            return results['rData']
        
        return ""

    # A helper method that handles the Python urllib2 code and basic HTTP protocol handling
    def _req(self, url):
       
        if (urlparse(url)[0] != 'http'):
            self.logger.error("Invalid HTTP url passed to 'request' method")
            return

        httpReq = urllib2.Request(url)
        # This is a "politeness" policy when talking to the webserver, the actual value doesn't really matter
        httpReq.add_header("User-Agent", self.__class__.defaultUserAgent)
        # Tell the webserver this script supports gzip compression
        httpReq.add_header("Accept-encoding","gzip")
        try:
            httpOpener = urllib2.urlopen(httpReq)
        except urllib2.URLError, err:
            self.logger.error(url+" "+str(err))
            return
        results = {}
        results['rData'] = httpOpener.read()
 
        if hasattr(httpOpener, 'headers'):
            #Check if the webserver is responding with a gzip'd document and handle it
            if(httpOpener.headers.get('content-encoding','') == 'gzip'):
                results['rData'] = gzip.GzipFile(fileobj = StringIO(results['rData'])).read()
        if hasattr(httpOpener, 'url'):
            results['rUrl'] = httpOpener.url
            results['rStatus'] = 200
        if hasattr(httpOpener, 'status'):
            results['rStatus'] = httpOpener.status

        return results

class CloudAggregator(Loggable):
    """
    This class is responsible for querying remote Cloud sites, retrieving their resource and real time XML,
    aggregate and validate the retrieved XML and then finally storing the aggregated XML into a RedisDB
    """
    # Since this is the "primary" class, it is designed to be instantiated first and thus will load the 
    # global ConfigMapping data
    def __init__(self):
        Loggable.__init__(self, self.__class__.__name__)
        loadConfig(self.logger) 
        
        #Connect to the RedisDB
        self.storageDb = Redis(db=ConfigMapping[TARGET_REDIS_DB], host=ConfigMapping[REDISDB_SERVER_HOSTNAME], port=int(ConfigMapping[REDISDB_SERVER_PORT]))

        # Verify the DB is up and running
        try:
           self.storageDb.ping()
           self.logger.debug("RedisDB server alive")
        except ConnectionError, err:
            print str(err)
            self.logger.error("ConnectionError pinging DB - redis-server running on desired port?")
            sys.exit(RET_CRITICAL)
  
    # This method will combine the real time XML data with the standard resource/Cloud data and return a 
    # single XML document. If no real time data is present in the XML then just the resource/Cloud XML is
    # returned. 
    
    def aggregateRealTimeData(self, cloudXML, rtCloudXML, cloudAddress):

        cloudDom = xml.dom.minidom.parseString(cloudXML)
        rtCloudDom = xml.dom.minidom.parseString(rtCloudXML)  

        for curNode in rtCloudDom.getElementsByTagName(TIME_STAMP):
            #print curNode.toxml()
            xmlTimeStamp = int( curNode.firstChild.nodeValue)
            if abs(xmlTimeStamp - int(str(time.time()).strip(".")[0])) > TIME_WINDOW:
                #pass
                self.logger.warning("Stale RealTime data received from Cloud at "+cloudAddress)

        # The below 2 lines are the W3C method for removing the "current node" from a DOM tree
        tNode = rtCloudDom.getElementsByTagName(TIME_STAMP)[0] 
        # Unlink the node from the DOM tree after we remove it since it will never be referenced again
        tNode.parentNode.removeChild(tNode).unlink()

        for curNode in rtCloudDom.firstChild.childNodes:
            # temp now contains the XML nodes encompassed by the <RealTime>...</RealTime> tags
            # but not the tags themselves (Public XML Schema/Format does not include <RealTime>...</RealTime>)
            temp = cloudDom.importNode(curNode, True) # True here means do a deep copy

            cloudDom.firstChild.appendChild(temp)

        return cloudDom.toxml()
 
    # Remotely query the configured server (in your servers file) with the configured paths for both real time and 
    # static XML data 
    def queryRemoteClouds(self):

        addrList = self.loadTargetAddresses()
        tempDict = {}

        dataReq = CloudAggregatorHTTPRequest()
        
        for entry in addrList:
            tempDict[entry] = {}
            
            if TARGET_XML_PATH in ConfigMapping:
                tempDict[entry][ConfigMapping[TARGET_XML_PATH]] = dataReq.request(entry+ConfigMapping[TARGET_XML_PATH])
            else:
                self.logger.error("No XML Path configured for host: "+entry)

            if TARGET_VM_SLOTS_PATH in ConfigMapping:
                tempDict[entry][ConfigMapping[TARGET_VM_SLOTS_PATH]] = dataReq.request(entry+ConfigMapping[TARGET_VM_SLOTS_PATH])
            else:
                self.logger.error("No Real Time XML Path configured for host: "+entry)
        #print tempDict    
        return tempDict


    # Libxml2 validation against an XSD Schema
    def validateXML(self, xmlToProcess):

        ctxtParser = libxml2.schemaNewParserCtxt(XSD_SCHEMA)
        ctxtSchema = ctxtParser.schemaParse()
        ctxtValid = ctxtSchema.schemaNewValidCtxt()

        doc = libxml2.parseDoc(xmlToProcess)
        retVal = doc.schemaValidateDoc(ctxtValid)
        if( retVal != 0):
            self.logger.error("Error validating against XML Schema - "+XSD_SCHEMA)
            sys.exit(RET_CRITICAL)
        
        doc.freeDoc()
        del ctxtValid
        del ctxtSchema
        del ctxtParser
        libxml2.schemaCleanupTypes()
        libxml2.cleanupParser()

    def persistData(self, cloudDict, cloud):

        skyXML = StringIO()
        skyXML.write("<?xml version=\"1.0\" encoding=\"UTF-8\"?>")
        skyXML.write("<Sky>")
 
        # Strip the XML Header declaration from Cloud doc before appending to the Sky doc
        if(cloud != None):
            skyXML.write(cloud[cloud.find("?>")+2:])
        skyXML.write("</Sky>")

        #print skyXML.getvalue()
        #Validate the final 'Sky' XML containing all the cloud information against the XML Schema
        self.validateXML(skyXML.getvalue())

        # Finally, save the valid XML into the database with the well known SKY_KEY
        self.storageDb.set(ConfigMapping[CLOUDS_KEY], skyXML.getvalue(), preserve=False)

        for key in cloudDict.keys():
            for subKey in cloudDict[key].keys():
            
               tagIndex = (cloudDict[key][subKey]).find("?>") + 2 
               # Persist the individual, aggregated cloud XML data into the DB with the path used to find
               # the XML as the key
               self.storageDb.set(key+subKey, cloudDict[key][subKey][tagIndex:], preserve=False)

        print self.storageDb.get(ConfigMapping[CLOUDS_KEY])

    # This method will extract the remote cluster addresses from the configured Clusters_Addr_File option in the 
    # sky_aggregator.cfg file
    def loadTargetAddresses(self):

        cloudAddresses = []

        if(os.path.exists(ConfigMapping[TARGET_CLOUDS_LOC])):
            try:
                fileHandle = open(ConfigMapping[TARGET_CLOUDS_LOC],"r")

                for addr in fileHandle:
                    cloudAddresses.append(addr.strip())
                fileHandle.close()
            except IOError, err:
                self.logger.error("IOError processing "+ConfigMapping[TARGET_CLUSTERS_LOC]+" - "+str(err))
        else:
            self.logger.error("Unable to find a filesystem path for Clusters_Addr_File configured in sky_aggregator.cfg")

        return cloudAddresses

if __name__ == "__main__":
   
    loader = CloudAggregator()
    while True:
        daDict = loader.queryRemoteClouds()
        for entry in daDict.keys():

            loader.persistData(daDict, loader.aggregateRealTimeData(daDict[entry][ConfigMapping[TARGET_XML_PATH]], daDict[entry][ConfigMapping[TARGET_VM_SLOTS_PATH]], entry))

        time.sleep(int(ConfigMapping[UPDATE_INTERVAL]))

