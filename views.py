import time
from flask import render_template, jsonify, request
from multiprocessing import Lock
from keras.models import load_model
from keras.preprocessing.text import text_to_word_sequence
import os, sys
import pickle
import h5py
from rnn_model import hinge_rank_loss
import ipdb
import numpy as np
from flask import Flask
import tensorflow as tf
import random
import argparse


parser = argparse.ArgumentParser(description='server')
parser.add_argument('-word_index', type=str, help="location of the DICT_word_index.VAL/TRAIN.pkl", required=True)
parser.add_argument("-cache", type=str, help="location of the cache.h5 file", required=True)
parser.add_argument("-model", type=str, help="location of the model.hdf5 snapshot", required=True)
parser.add_argument("-threaded", type=int, help="Run flask server in multi-threaded/single-threaded mode", required=True)
parser.add_argument("-host", type=str, help="flask server host in app.run()", required=True)
parser.add_argument("-port", type=int, help="port on which the server will be run", required=True)
parser.add_argument("-dummy", type=int, help="run server in dummy mode for testing js/html/css etc.", required=True)
parser.add_argument("-captions_train", type=str, help="location of string captions of training images", required=True)
parser.add_argument("-captions_valid", type=str, help="location of string captions of validation images", required=True)
args = parser.parse_args()

app = Flask(__name__)

DUMMY_MODE = bool(args.dummy)
MODEL_LOC = args.model
WORD_DIM = 300

# VERY IMPORTANT VARIABLES
mutex = Lock()
MAX_SEQUENCE_LENGTH = 20
MODEL=None
DICT_word_index = None
if DUMMY_MODE==False:
	
	MODEL = load_model(MODEL_LOC, custom_objects={"hinge_rank_loss":hinge_rank_loss})
	graph = tf.get_default_graph()
	
	print MODEL.summary()
	
	assert os.path.isfile(args.word_index), "Could not find {}".format(args.word_index)	
	
	with open(args.word_index,"r") as f:
		DICT_word_index = pickle.load(f)
	assert DICT_word_index is not None, "Could not load dictionary that maps word to index"

	im_outs = None 
	fnames = None
	with h5py.File(args.cache) as F:
		im_outs = F["data/im_outs"][:]
		fnames  = F["data/fnames"][:]
	assert im_outs is not None, "Could not load im_outs from cache.h5"
	assert fnames is not None, "Could not load fnames from cache.h5"

	# load the string captions from .json file 

	from pycocotools.coco import COCO
	train_caps = COCO(args.captions_train)
	valid_caps = COCO(args.captions_valid)


def get_string_captions(results_url):
	''' input -> results_url (https://mscoco.org/3456) 
		output -> string_captions corresponding to each result in result_url 
	'''
	result_captions = []
	for result in result_url:
		
		annIds = cocoObj.train_caps(imgIds=int(result.split("/")[-1])) # try and find image in train_caps
		if len(annIds) == 0:
			annIds = cocoObj.valid_caps(imgIds=int(result.split("/")[-1]))	# if you can't, find it in valid_caps
		assert len(annIds) > 0, "Something wrong here, could not find any caption for given image"
		
		anns = coco_caps.loadAnns(annIds)
		ann  = str(random.choice(anns)["caption"])
		result_captions.append(ann)

	return result_captions

# Query string -> word index list 
def query_string_to_word_indices(query_string):
	
	# string -> list of words 
	words = text_to_word_sequence(
			text = query_string,
			filters='!"#$%&()*+,-./:;<=>?@[\\]^_`{|}~\t\n',
			lower=True,
			split=" "
		)

	# check if words in dictionary
	all_words = DICT_word_index.keys()
	for word in words:
		if word not in all_words: 
			_err_msg = "could not find word  | {} | in server's dictionary".format(word)
			raise ValueError(_err_msg)

	# list of words -> list of indices
	words_index = []
	for word in words:
		words_index.append(DICT_word_index[word])

	# pad to 20 words
	if len(words_index) < MAX_SEQUENCE_LENGTH:
		padding = [0 for _ in range(MAX_SEQUENCE_LENGTH - len(words_index))]
		words_index += padding

	if len(words_index) != MAX_SEQUENCE_LENGTH:
		raise ValueError("words_index is not {} numbers long".format(MAX_SEQUENCE_LENGTH))

	return np.array(words_index).reshape((1,MAX_SEQUENCE_LENGTH))

@app.route("/")
@app.route("/index")
def index():
	return render_template("index.html", title="Home")

def run_model(query_string):
	''' This fxn takes a query string
	runs it through the Keras model and returns result.'''

	# run forward pass
	# find diff 
	# get images having closest diff
	print "..waiting to acquire lock"
	result = None
	with mutex:
		print "lock acquired, running model..."
		if DUMMY_MODE:
			time.sleep(10)
			result = ["static/dog.jpg", "static/dog.jpg", "static/dog.jpg"]
		else:
			assert MODEL is not None, "not in dummy mode, but model did not load!"

			# convert query string to word_index
			try:
				word_indices = query_string_to_word_indices(query_string)
			except Exception, e:
				print str(e)
				return 2, str(e)

			## multithread fix for keras/tf backend
			global graph
			with graph.as_default():
				# forward pass 
				output = MODEL.predict([ np.zeros((1,4096)) , word_indices ])[:, WORD_DIM: ]
				output = output / np.linalg.norm(output, axis=1, keepdims=True)
			
				# compare with im_outs
				TOP_K = 50
				diff = im_outs - output 
				diff = np.linalg.norm(diff, axis=1)
				top_k_indices = np.argsort(diff)[:TOP_K].tolist()

				# populate "results" with fnames of top_k_indices
				result = []
				for k in top_k_indices:
					result.append(fnames[k][0])

				# Replace /var/coco/train2014_clean/COCO_train2014_000000364251.jpg with http://mscoco.org/images/364251
				result_url = []
				for r in result:
					imname = r.split("/")[-1] # COCO_train2014_000000364251.jpg
					imname = imname.split("_")[-1] # 000000364251.jpg
					
					i = 0
					while imname[i] == "0":
						i += 1
					imname = imname[i:] # 364251.jpg
					imname = imname.rstrip(".jpg") # 364251
					imname = "http://mscoco.org/images/" + imname # http://mscoco.org/images/364251

					result_url.append(imname)
				result = result_url 

			
		print '..over'
	
	if result is None or len(result)<2:
		return 1,"oops! something went wrong. model prediction returned None. Note: We should probably re-factor our code."
	else:
		return 0,result

@app.route("/_process_query")
def process_query():

	query_string = request.args.get('query', type=str)
	rc, images = run_model(query_string) 

		
	result = {
		"rc":rc,
		"images": images
	}

	return jsonify(result)

if __name__ == '__main__':
	app.run(threaded=bool(args.threaded), host=args.host, port=args.port)
