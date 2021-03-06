##############################################
#  IMPORT STATEMENTS
##############################################

# == Native ==
import os
import sys
import json
import copy
import json
import datetime
import logging
from typing import Dict, List, Any, Tuple, Hashable, Iterable, Union
import functools
import ast

# == Flask ==
from flask import Flask, jsonify, session
from flask_cors import CORS

# == Pymongo ==
from pymongo import MongoClient
from bson.objectid import ObjectId

# == Local ==
from utils import database_uri_compose, stringify
from annotator_config import Configuration
from annotator import DialogueAnnotator

class DatabaseManagement(object):

##############################################
#  INIT
##############################################

	#importing json configuration file
	conf = Configuration.conf

	#the uri, if present, will override the legacy configuration
	databaseURI = database_uri_compose(conf["database"])

	#connecting
	client = MongoClient(databaseURI)
	try:
		logging.info(" * MATILDA: Connecting to database "+str(databaseURI))
		client.server_info()
	except Exception as e: 
		logging.warning(" * "+str(e)+"\n * Error connecting to Database. Trying again with legacy configuration...")
		conf["database"]["optional_uri"] = None
		databaseURI = database_uri_compose(conf["database"])
		client = MongoClient(databaseURI)

	db = client[conf["database"]["name"]]

	users = db["users"]
	dialogueCollections = db["dialogues_collections"]
	annotatedCollections = db["annotated_collections"]


##############################################
#  METHODS
##############################################

	def selected(collection):
		if collection == "dialogues_collections":
			return DatabaseManagement.dialogueCollections
		elif collection == "annotated_collections":
			return DatabaseManagement.annotatedCollections
		else:
			return DatabaseManagement.users

	def readDatabase(coll,pairs=None, projection=None):
		# if pairs parameter is provided the search will be a projection of the id
		# and the requested fields.
		# if string is provided the search will be restricted to the string match.
		# last parameter allows to restrict response to desired fields

		responseObject = []

		selected_collection = DatabaseManagement.selected(coll)

		#logging.info(" * Searching in:",coll,"for key '",pairs)

		documentLengthOnly = False

		#adds restrictions to the search
		if pairs is None:
			pairs = {}
		
		#search with projection of interested fields or simple search
		if projection is not None:
			try:
				if projection["document"] == "length":
					projection["document"] = 1
					documentLengthOnly = True
			except:
				pass
			query = selected_collection.find(pairs,projection)
		else:
			query = selected_collection.find(pairs)

		#operations after the query
		for line in query:
			#convert objectId into string
			if line.get("_id") is not None:
				line["_id"] = str(line["_id"])
			#calculate document length which also is dialogues total number	
			if line.get("document") is not None:
				line["documentLength"] = len(line["document"])
			#remove document if only length is requested
			if documentLengthOnly:
				line["document"] = line["documentLength"]

			responseObject.append(line)

		return responseObject

	def deleteDoc(collection, pair):

		#delete a database document by id

		if "_id" in pair:
			pair["_id"] = ObjectId(pair["_id"])

		DatabaseManagement.selected(collection).delete_one(pair)

		responseObject = { "status":"success" }
		return responseObject

	def createDoc(document_id, collection, values):
		
		#logging.info(" * Creating document", document_id, "in",collection)
		DatabaseManagement.selected(collection).save(values)
		
		response = {"staus":"success"}
		return response 

	def updateDoc(searchFields, collection, updateFields):

		DatabaseManagement.selected(collection).update(searchFields, { "$set": updateFields })

	def updateDocs(searchFields, collection, updateFields):
		
		DatabaseManagement.selected(collection).update_many(searchFields, { "$set": updateFields })


	def pullFromDoc(doc_id, collection, field):

		value = field["dialogue"]

		mainDocument = (DatabaseManagement.readDatabase("dialogues_collections", {"id":doc_id}, {"document":1}))
		annotatedDocuments = (DatabaseManagement.readDatabase("annotated_collections", {"id":doc_id}, {"document":1}))

		for i in range(len(annotatedDocuments)):
			del annotatedDocuments[i]["document"][value]
			DatabaseManagement.selected("annotated_collections").update(
				{"_id":ObjectId(annotatedDocuments[i]["_id"])}, 
				{ "$set": {
					"document":annotatedDocuments[i]["document"], 
					"documentLength":len(annotatedDocuments[i]["document"])
				}
			})

		del mainDocument[0]["document"][value]
		DatabaseManagement.selected("dialogues_collections").update(
			{ "id":doc_id }, 
			{ "$set": {
				"document":mainDocument[0]["document"], 
				"documentLength":len(mainDocument[0]["document"])
				} 
			})

		return {"status":"success"}


	def dumpDatabase():
		collections = DatabaseManagement.db.collection_names()
		dump = {}
		for i, collection_name in enumerate(collections):
			col = getattr(DatabaseManagement.db,collections[i])
			collection = col.find()
			dump[collection_name] = []
			for document in collection:
				for attribute in document:
					document[attribute] = stringify(document[attribute])
				dump[collection_name].append(document)
		return dump

###############################################
# ANNOTATIONS AND DIALOGUE-COLLECTIONS UPDATE
################################################

	def storeAnnotations(username, destination, fields, backup=None):
		#update the database user's document
		annotations = DialogueAnnotator.get_dialogues(DialogueAnnotator,username)

		#if back up mode then saves with a different id and 
		# checks if document will be empty before saving
		if backup:
			if annotations == {}:
				responseObject = {"status":"empty"}
				return responseObject

		#saving or updating
		if len(DatabaseManagement.readDatabase("annotated_collections",{"id":destination, "annotator":username})) == 0:
			values = {
				"id":destination, 
				"fromCollection":destination, 
				"annotator":username, 
				"done":False, 
				"status":fields["status"],
				"document":annotations,
				"lastUpdate":datetime.datetime.utcnow()
			}
			#print(" * Creating document", destination, "in annotated_collections")
			DatabaseManagement.createDoc(destination, "annotated_collections", values)
		else:
			#print(" * Updating document", destination, "in annotated_collections")
			values = { "status":fields["status"], "document":annotations, "lastUpdate":datetime.datetime.utcnow() }
			DatabaseManagement.updateAnnotations(username, destination, values)
		
		responseObject = {"status":"success"}
		return responseObject	

	def updateAnnotations(username, destination, fields):
		DatabaseManagement.selected("annotated_collections").update(
			{ "id":destination, "annotator":username }, { "$set": fields })


class LoginFuncs(object):

	loggedUser = {}

	administratorDefault = {
		"id":"admin",
		"userName":"admin",
		"password":"admin",
		"email":"",
		"role":"administrator"
	}

	def logIn(userID, userPass):

		response = { "status":"fail" }

		query = {"userName":userID,"password":userPass}

		userDetails = DatabaseManagement.users.find_one(query)

		if userDetails != None:
			if userDetails["userName"] == userID:
				if userDetails["password"] == userPass:
					session['userName'] = userID
					session['token'] = os.urandom(6)
					LoginFuncs.loggedUser[userID] = session['token']
					logging.info(" * New session for "+userID)
					response = { "status":"success", "role":userDetails["role"] }

		return response

	def logOut(userID):
		session.clear()
		return { "status": "done" }

	def checkSession():
		try:
			if session['userName']:
				if LoginFuncs.loggedUser[session['userName']] != session['token']:
					return False
		except:
			return False
		return True

	def start():
		if DatabaseManagement.users.count_documents({"id":"admin"}) == 0:
			DatabaseManagement.users.insert_one(LoginFuncs.administratorDefault)
			logging.warning(" * Default admin account created: please log-in with username 'admin' and password 'admin'")
		else:
			logging.warning(" * Connected to database "+str(DatabaseManagement.databaseURI))

