import torch
from torch.utils.data import Dataset, DataLoader
import torchvision.transforms as transforms

from PIL import Image
from PIL import ImageFile
from modules.language_encoder import get_language_encoder, BertTokenizer
import pandas as pd
import config
import os
import numpy as np
import pickle
from transformers import DeiTFeatureExtractor
import ast
import random 

class IQA_Dataset(Dataset):
	def __init__(self, dataset_folder, annotation_file, mode="train", num_answers=18):
		anno_df = pd.read_csv(annotation_file)
		self.annotations = anno_df.loc[anno_df['mode']==mode]
		self.image_folder_path = dataset_folder + mode + '2014/'
		self.mode = mode
		self.num_answers = num_answers
		self.resize = transforms.Resize(config.image_size)
		self.norm = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
		self.to_tensor = transforms.ToTensor()
		self.tokenize = BertTokenizer().tokenize_text
		if config.feature_extractor == "deit":
			self.deit_features = DeiTFeatureExtractor.from_pretrained("facebook/deit-base-distilled-patch16-224")

	def __len__(self):
		# if self.mode == "train":
		# 	return 20000
		# elif self.mode == "val":
		# 	return 2000
		return self.annotations.shape[0]

	def __getitem__(self, idx):
		sample = self.annotations.iloc[[idx]]
		image_name = str(sample["image_id"].item())
		image_full_name = "COCO_" + self.mode + "2014_"+"0"*(12-len(image_name)) + image_name + ".jpg"
		
		image_path = os.path.join(self.image_folder_path, image_full_name)

		image = Image.open(image_path).convert('RGB')

		if config.feature_extractor == "deit":
			image_tensor = self.deit_features(image, return_tensors="pt")["pixel_values"]
			#  = self.to_tensor(self.resize(image)) # [3, config.image_size, config.image_size]
		else:
			image_tensor = self.norm(self.to_tensor(self.resize(image))) # [3, config.image_size, config.image_size]
		
		# question_tokens [1, config.padded_language_length_question]
		# question_mask [1, config.padded_language_length_question]
		question_tokens, question_mask = self.tokenize(sample['question'].item(), max_length=config.padded_language_length_question)

		ground_truth = sample['ground_truth'].item()
		gt_tokens, gt_mask = self.tokenize(ground_truth, max_length=config.padded_language_length_answer)

		false_answers = ast.literal_eval(sample['multiple_choices'].item())
		false_answers.remove(ground_truth)

		answer_tuples = [(ground_truth, gt_tokens, gt_mask)]

		for _ in range(self.num_answers - 1):
			rand_index = random.randint(0, len(false_answers)-1)
			answer = false_answers[rand_index] # select a random false answer that hasnt been selected before
			answer_tokens, answer_mask = self.tokenize(answer, max_length=config.padded_language_length_answer)

			answer_tuples.append((answer, answer_tokens, answer_mask))

			false_answers.remove(answer) # remove selected answer so it doesnt get picked again

		random.shuffle(answer_tuples)

		answer_list, answer_tokens, answer_masks = map(list, zip(*answer_tuples))
		answer_tokens, answer_masks = torch.stack(answer_tokens), torch.stack(answer_masks)

		return image_tensor, question_tokens, question_mask, answer_tokens, answer_masks, answer_list.index(ground_truth)

class TGIF_Dataset(Dataset):
	def __init__(self, dataset_folder, annotation_file, mode="train", num_answers=5):
		self.annotations = pd.read_csv(annotation_file)
		self.image_folder_path = dataset_folder + 'tgif_image_features/'
		self.qa_folder_path = dataset_folder + 'tgif_text_features/'
		self.mode = mode

	def __len__(self):
		return self.annotations.shape[0]

	def index_to_tensor(self, index, total_length):
		return torch.cat([torch.ones(index), torch.zeros(total_length-index)])

	def __getitem__(self, idx):
		sample = self.annotations.iloc[[idx]]
		image_name = sample["gif_name"].item()

		image_path = os.path.join(self.image_folder_path, image_name)+".pkl"
		qa_path = os.path.join(self.qa_folder_path, image_name)+".pkl"

		with open(image_path, "rb") as handle:
			image_data = pickle.load(handle)

		with open(qa_path, "rb") as handle:
			qa_data = pickle.load(handle)


		ret = []

		image_tensor = image_data["tensor"]
		image_mask = self.index_to_tensor(image_data["mask"], len(image_tensor))

		ret.append(image_tensor)
		ret.append(image_mask)


		question_tensor = qa_data['question']
		question_mask = self.index_to_tensor(qa_data['question_mask_idx'], question_tensor.shape[0])

		ret.append(question_tensor)
		ret.append(question_mask)

		answers = []
		answer_masks = []
		for c in ["a1", "a2", "a3", "a4", "a5"]:
			tensor = qa_data[c]
			a_mask = self.index_to_tensor(qa_data[f"{c}_mask_idx"], len(tensor))
			answers.append(tensor)
			answer_masks.append(a_mask)

		ret.append(torch.stack(answers))
		ret.append(torch.stack(answer_masks))

		ret.append(sample['answer'].item()) # ground truth

		return ret

class text_preproc(Dataset):
	def __init__(self, annotation_file, mode="train"):
		self.annotation_file = annotation_file
		self.dataset = pd.read_csv(annotation_file)

	def __len__(self):
		return len(self.dataset)

	def __getitem__(self, idx):
		sample = self.dataset.iloc[idx]
		return sample["gif_name"], sample["question"], [sample["a1"], sample["a2"], sample["a3"], sample["a4"], sample["a5"]]

class GIF_preproc(Dataset):
	def __init__(self, image_folder, mode="train"):
		self.image_folder_path = image_folder
		self.filenames = [i.split(".gif")[0] for i in os.listdir(self.image_folder_path) if i.endswith(".gif")]
		self.to_tensor = transforms.ToTensor()
		self.resize = transforms.Resize(config.image_size)
		self.mode = mode
		ImageFile.LOAD_TRUNCATED_IMAGES = True

	def __len__(self):
		return len(self.filenames)

	def get_image_frames(self, path):
		gif = Image.open(path)
		# We can access the number of frames using gif.n_frames 
		image_list = []

		# Calculation to account for different numbers of frames and how frames should be 'skipped'
		step = 1 if gif.n_frames < config.padded_frame_length else gif.n_frames/config.padded_frame_length		
		for f in np.arange(0, gif.n_frames, step):
			gif.seek(int(f))
			frame = self.resize(gif).convert('RGB')
			image_tensor = self.to_tensor(frame).squeeze()
			image_list.append(image_tensor)

		# Check if we need to pad
		needed_padding = config.padded_frame_length - len(image_list)

		# Add empty images if needed
		image_list.extend([torch.zeros_like(image_tensor) for _ in range(needed_padding)])

		# Create the mask (not used)
		mask = torch.cat([torch.ones(len(image_list)), torch.zeros(needed_padding)])

		# We return the images, and the index where the padding starts
		return torch.stack(image_list), len(image_list)-needed_padding

	def __getitem__(self, idx):
		image_name = self.filenames[idx]
		
		image_frames, masking_idx = self.get_image_frames(os.path.join(self.image_folder_path, image_name)+".gif")

		return image_name, image_frames, masking_idx


def get_dataset(data_source="TGIF", dataset_folder=None, annotation_file=None):
	if not (dataset_folder and annotation_file) and data_source=="TGIF":
		raise Exception("Both image_folder and annotation_file location are required, 1 or both not passed")

	modes = ["train", "val"] # "test"

	if data_source=="TGIF":
		dataset_class = TGIF_Dataset

	elif data_source=="IQA":
		dataset_class = IQA_Dataset


	elif data_source=="GIF_preproc":
		return DataLoader(
			GIF_preproc(dataset_folder),
			batch_size=config.batch_size,
			shuffle=True,
			num_workers=0)
	
	elif data_source=="text_preproc":
		return DataLoader(
			text_preproc(annotation_file),
			batch_size=config.batch_size,
			shuffle=True,
			num_workers=0)

	else:
		raise Exception("data source not recognised:", data_source)

	return [DataLoader(
		dataset_class(dataset_folder, annotation_file, mode, config.num_answers),
		batch_size=config.batch_size,
		shuffle = False, # we only shuffle the training set, not the validation
		num_workers=0)
		for mode in modes]




#
# TGIF dataset that calculates encodings for each image and text at run time
#

# class old_TGIF_dataset(Dataset):
# 	def __init__(self, image_folder, annotation_file, mode="train"):
# 		self.annotations = pd.read_csv(annotation_file, sep='\t', header=0)
# 		self.image_folder_path = image_folder
# 		self.to_tensor = transforms.ToTensor()
# 		self.resize = transforms.Resize(config.image_size)
# 		self.mode = mode

# 	def __len__(self):
# 		return self.annotations.shape[0]

# 	def get_image_frames(self, path):
# 		gif = Image.open(path)
# 		# We can access the number of frames using gif.n_frames 
# 		image_list = []

# 		# Calculation to account for different numbers of frames and how frames should be 'skipped'
# 		step = 1 if gif.n_frames < config.padded_frame_length else gif.n_frames/config.padded_frame_length		
# 		for f in np.arange(0, gif.n_frames, step):
# 			gif.seek(int(f))
# 			frame = self.resize(gif).convert('RGB')
# 			image_tensor = self.to_tensor(frame).squeeze()
# 			image_list.append(image_tensor)

# 		# Check if we need to pad
# 		needed_padding = config.padded_frame_length - len(image_list)

# 		# Add empty images if needed
# 		image_list.extend([torch.zeros_like(image_tensor) for _ in range(needed_padding)])

# 		# Create the mask
# 		mask = torch.cat([torch.ones(len(image_list)), torch.zeros(needed_padding)])

# 		return torch.stack(image_list)

	# def __getitem__(self, idx):
	# 	sample = self.annotations.iloc[[idx]]
	# 	image_path = sample["gif_name"].item()
	# 	question = sample['question'].item()
	# 	ground_truth = sample['answer'].item()
	# 	answer_choices = [sample[f'a{i}'].item() for i in range(1,6)]

	# 	# image_path = self.image_folder_path + 'test.gif' # TODO: self.image_folder_path + sample['gif_name'].iloc[0] + '.gif'

	# 	# t = time.process_time()
	# 	image_frames = self.get_image_frames(os.path.join(self.image_folder_path, image_path)+".gif") # TODO: avoid calculating the gif frames here, instead should be done for every gif in init()
	# 	# self.time += time.process_time() - t
	# 	# print(self.time)

	# 	return image_frames, question, answer_choices, ground_truth