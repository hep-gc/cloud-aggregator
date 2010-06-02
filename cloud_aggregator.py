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
from cloud_logger import Logger
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

# 600 seconds is 10 minutes - the window for "fresh" real time data
# This # is aribitrary
TIME_WINDOW = 600
TIME_STAMP = "TimeStamp"

XSD_SCHEMA = "clouds.xsd"

#MONITORING_XML_LOC = "Local_Monitoring_XML_Data"
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
#            ConfigMapping[MONITORING_XML_LOC] = cfgFile.get(CONF_FILE_SECTION,MONITORING_XML_LOC,0)
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


class CloudAggregatorHTTPRequest:
    """
       This class provides very basic HTTP GET functionality. It was created as a helper class for cloud_aggregator to use
       to query remote Clouds. It implements basic HTTP protocol functionality including gzip support. However, any HTTP 
       status returned other than 200/OK is not supported and will be considered an error
    """

    # This can be almost anything, this was chosen arbitrarily. One could masquerade as another browser or
    # agent, but I don't see the need for this currently
    defaultUserAgent = "CloudAggregator/1.0"

    def __init__(self, logger):
        self.logger = logger

    # Request the data from the url passed in. This is done with a HTTP GET and the return code is checked to ensure
    # a 200 (sucess) was received
    def request(self, url):

        results = self._req(url)
        if results != None:  
            # If anything other than 'OK'/200 is received, log an error as this code doesn't support any fancy HTTP stuff
            if results['rStatus'] != 200:
                # BaseHTTPRequestHandler includes a mapping of HTTP return codes to a brief text description
                self.logger.error("Received HTTP Code: "+str(results['rStatus'])+" - "+ BaseHTTPRequestHandler.responses[results['rStatus']][0])      
            return results['rData']
        # To prevent "None" from being returned and causing headaches for the caller
        return ""

    # A helper method that handles the Python urllib2 code and basic HTTP protocol handling
    # Only basic HTTP get is implemented, and functionality such as redirects and other HTTP protocol handling is NOT implemented    

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
        # Here the actual data is gathered from the web server and saved
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

class CloudAggregator:
    """
    This class is responsible for querying remote Cloud sites, retrieving their resource and real time XML,
    aggregate and validate the retrieved XML and then finally storing the aggregated XML into a RedisDB
    """
    # Since this is the "primary" class, it is designed to be instantiated first and thus will load the 
    # global ConfigMapping data
    def __init__(self, logger=None):
        if(logger):
            self.logger = logger
        else:
            self.logger = Logger("cloud_aggregator", "cloud_aggregator.log")
        loadConfig(self.logger) 
        
        #Connect to the RedisDB with the configured options
        self.storageDb = Redis(db=ConfigMapping[TARGET_REDIS_DB], host=ConfigMapping[REDISDB_SERVER_HOSTNAME], port=int(ConfigMapping[REDISDB_SERVER_PORT]))

        # Verify the DB is up and running
        try:
           self.storageDb.ping()
           self.logger.debug("RedisDB server alive")
        except ConnectionError, err:
#            print str(err)
            self.logger.error("redis-server running on desired port? "+str(err))
            sys.exit(RET_CRITICAL)
  
    # This method will combine the real time XML data with the standard resource/Cloud data and return a 
    # single XML document. If no real time data is present in the XML then just the resource/Cloud XML is
    # returned. 
    
    def aggregateRealTimeData(self, cloudXML, rtCloudXML, cloudAddress):

        # Create a DOM representation (in memory)  of both XML documents
        cloudDom = xml.dom.minidom.parseString(cloudXML)
        rtCloudDom = xml.dom.minidom.parseString(rtCloudXML)  
        
        # A "hidden" timestamp tag is included with the RealTime XML to alert the cloud_aggregator if the 
        # data is actually old and hasn't been updated recently. This means it is not really "RealTime" anymore

        # Find the timestamp tag - There should be 1 only
        for curNode in rtCloudDom.getElementsByTagName(TIME_STAMP):
            # Caclulate if the Real Time (rt) data received was generated within the configured time window
            # Hopefully all servers are configured using NTP or something similar to ensure equivalent clocks
            # across the different hosts
            xmlTimeStamp = int( curNode.firstChild.nodeValue)
            
            if abs(xmlTimeStamp - int(str(time.time()).split(".")[0])) > TIME_WINDOW:
                self.logger.warning("Stale RealTime data received from Cloud at "+cloudAddress)

        # The below 2 lines are the W3C method for removing the "current node" from a DOM tree
        # It looks a little strange to reference the parent from the child only to reference the child again, but 
        #  this is necessary for the removeChild call to function properly
        tNode = rtCloudDom.getElementsByTagName(TIME_STAMP)[0] 
        # Unlink the node from the DOM tree after we remove it since it will never be referenced again
        tNode.parentNode.removeChild(tNode).unlink()

        for curNode in rtCloudDom.firstChild.childNodes:
            # temp now contains the XML nodes encompassed by the <RealTime>...</RealTime> tags
            # but not the tags themselves (Public XML Schema/Format does not include <RealTime>...</RealTime>)
            temp = cloudDom.importNode(curNode, True) # True here means do a deep copy
            # The placement the XML doesn't really matter, so just append it to the first child 
            cloudDom.firstChild.appendChild(temp)

        return cloudDom.toxml()
 
    # Remotely query the configured server (in your servers file) with the configured paths for both real time and 
    # static XML data 
    # The data structure returned is a dictionary with the cloud remote address as the first key and the path used
    # to query the XML as the secondary key
    def queryRemoteClouds(self):

        addrList = self.loadTargetAddresses()
        tempDict = {}

        dataReq = CloudAggregatorHTTPRequest(self.logger)
        
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


    # Libxml2 validation against an XSD Schema. This code was adapted from the example python code included with the libxml2 
    # python module
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
 
        # Strip the XML Header declaration from Cloud XML doc before appending to the Sky XML doc
        if(cloud != None):
            skyXML.write(cloud[cloud.find("?>")+2:])
        skyXML.write("</Sky>")

        #print skyXML.getvalue()
        #Validate the final 'Sky' XML containing all the cloud information against the XML Schema
        self.validateXML(skyXML.getvalue())

        # Finally, save the valid XML into the Redis DB with the configured CLOUDS_KEY
        self.storageDb.set(ConfigMapping[CLOUDS_KEY], skyXML.getvalue(), preserve=False)

        # Individual Cloud XML docs are saved in addition to the single, aggregated Sky XML
        for key in cloudDict.keys():
            for subKey in cloudDict[key].keys():
            
               # Again, we want to strip off the XML declaration before saving it into the DB
               tagIndex = (cloudDict[key][subKey]).find("?>") + 2 
               # Persist the individual, aggregated cloud XML from each cloud site into the DB with the path used 
               # to query the XML (cloud location + path) as the key
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
            self.logger.error("Unable to find a filesystem path for Clusters_Addr_File configured in cloud_aggregator.cfg")

        return cloudAddresses

if __name__ == "__main__":

    loader = CloudAggregator()
    while True:
        daDict = loader.queryRemoteClouds()
        # For each configured remote cloud, lookup it's XML, aggregate it then save it 
        for entry in daDict.keys():

            loader.persistData(daDict, loader.aggregateRealTimeData(daDict[entry][ConfigMapping[TARGET_XML_PATH]], daDict[entry][ConfigMapping[TARGET_VM_SLOTS_PATH]], entry))

        time.sleep(float(ConfigMapping[UPDATE_INTERVAL]))

