# Copyright 2022 The Otter Team.
# All rights reserved.
# This source code is licensed under the Apache 2.0 license
# found in the LICENSE file in the root directory.


import base64
from io import BytesIO
import re
import contextlib
import os

from PIL import ImageFile
from torchvision import transforms

import sys
# sys.path.append("/mnt/lustre/yhzhang/Otter/pipeline/multi_instruct_data_utils")
# from transforms import *
from .transforms import *


from .multi_instruct_dataset import (
    MultiInstructDataset,
    collate_fn,
)

# from multi_instruct_dataset import (
#     MultiInstructDataset,
#     collate_fn,
# )

import os

import json

label_map = {"entailment": 0, "not_entailment": 1}

IMAGENET_DEFAULT_MEAN = (0.485, 0.456, 0.406)
IMAGENET_DEFAULT_STD = (0.229, 0.224, 0.225)

FLAMINGO_MEAN = [0.481, 0.458, 0.408]
FLAMINGO_STD = [0.269, 0.261, 0.276]

ImageFile.LOAD_TRUNCATED_IMAGES = True
ImageFile.MAX_IMAGE_PIXELS = None
Image.MAX_IMAGE_PIXELS = None


@contextlib.contextmanager
def numpy_seed(seed, *addl_seeds):
    """Context manager which seeds the NumPy PRNG with the specified seed and
    restores the state afterward"""
    if seed is None:
        yield
        return
    if len(addl_seeds) > 0:
        seed = int(hash((seed, *addl_seeds)) % 1e6)
    state = np.random.get_state()
    np.random.seed(seed)
    try:
        yield
    finally:
        np.random.set_state(state)


class UnifyDataset(MultiInstructDataset):
    def __init__(self, args, is_test=False, supported_data_types=["caption", "qa"]):
        super().__init__(args, is_test)
        self.max_src_length = args.max_src_length
        self.max_tgt_length = args.max_tgt_length

        self.seed = args.pretrain_seed
        self.patch_image_size = args.patch_image_size
        self.supported_data_types = supported_data_types

        self.epoch = 0

        scales = [(args.patch_image_size, args.patch_image_size)]

        # TODO: check if random augment is correct, especially for some questions related to colors.
        self.patch_resize_transform = transforms.Compose(
            [
                RandomResize(scales),
                transforms.CenterCrop(args.patch_image_size),
                transforms.RandomHorizontalFlip(p=0.5),
                transforms.ToTensor(),
                transforms.Normalize(mean=FLAMINGO_MEAN, std=FLAMINGO_STD),
            ]
        )

        self.multi_instruct_path = args.multi_instruct_path
        self.images_path = args.images_path
        self.train_config_path = args.train_config_path

        assert os.path.exists(
            self.multi_instruct_path
        ), "Error: The local datafile {} not exists!".format(self.multi_instruct_path)

        assert os.path.exists(
            self.images_path
        ), "Error: The local datafile {} not exists!".format(self.images_path)


        assert os.path.exists(
            self.train_config_path
        ), "Error: The local datafile {} not exists!".format(self.train_config_path)

        with open(self.multi_instruct_path) as f:
            self.dataset = json.load(f)

        with open(self.images_path) as f:
            self.images = json.load(f)

        with open(self.train_config_path) as f:
            self.train_config = json.load(f)

        self.train_data_list = list(self.train_config.keys())

        self.bos_item = torch.LongTensor([args.tokenizer.bos_token_id])
        self.eos_item = torch.LongTensor([args.tokenizer.eos_token_id])
        self.bos_mask = torch.LongTensor([1])
        self.eos_mask = torch.LongTensor([1])

    def pre_question(self, question, max_ques_words):
        question = (
            question.lower().lstrip(",.!?*#:;~").replace("-", " ").replace("/", " ")
        )

        question = re.sub(
            r"\s{2,}",
            " ",
            question,
        )
        question = question.rstrip("\n")
        question = question.strip(" ")

        # truncate question
        question_words = question.split(" ")
        if len(question_words) > max_ques_words:
            question = " ".join(question_words[:max_ques_words])

        return question

    def pre_answer(self, answer, max_ans_words):
        answer = re.sub(
            r"\s{2,}",
            " ",
            answer,
        )
        answer = answer.rstrip("\n")
        answer = answer.strip(" ")

        # truncate question
        return_answer = ""
        answers = answer.split(".")

        for _ in answers:
            if return_answer == "":
                cur_answer = _
            else:
                cur_answer = ".".join([return_answer, _])
            if len(cur_answer.split(" ")) <= max_ans_words:
                return_answer = cur_answer
            else:
                break

        if return_answer == "":
            answer_words = answer.split(" ")
            return_answer = " ".join(answer_words[:max_ques_words])
        else:
            if return_answer[-1] != "." and return_answer != answers:
                return_answer += "."

        return return_answer

    def pre_caption(self, caption, max_words):
        caption = (
            caption.lower()
            .lstrip(",.!?*#:;~")
            .replace("-", " ")
            .replace("/", " ")
            .replace("<person>", "person")
        )

        caption = re.sub(
            r"\s{2,}",
            " ",
            caption,
        )
        caption = caption.rstrip("\n")
        caption = caption.strip(" ")

        # truncate caption
        caption_words = caption.split(" ")
        if len(caption_words) > max_words:
            caption = " ".join(caption_words[:max_words])

        return caption

    def set_epoch(self, epoch, **unused):
        self.epoch = epoch

    def process_image_text_pair(self, index):
        cur_train_id = self.train_data_list[index]
        (
            instruction_id,
            instruction,
            answer,
            image_ids,
            in_context_example_ids,
            split,
            dataset_name,
            type,
        ) = (
            self.dataset[cur_train_id]["instruction_id"],
            self.dataset[cur_train_id]["instruction"],
            self.dataset[cur_train_id]["answer"],
            self.dataset[cur_train_id]["image_ids"],
            self.train_config[cur_train_id]["in_context_example_ids"],
            self.dataset[cur_train_id]["split"],
            self.dataset[cur_train_id]["dataset_name"],
            self.dataset[cur_train_id]["type"],
        )
        # if type not in self.supported_data_types:
        #     return None
        self.max_src_length = self.max_tgt_length = 256

        patch_images = torch.tensor([])
        patch_masks = torch.tensor([])
        incontext_text = ""
        for cur_incontext_id in in_context_example_ids[:2]:
            cur_incontext_image_id = self.dataset[cur_incontext_id]["image_ids"]
            cur_incontext_instruction = self.dataset[cur_incontext_id]["instruction"]
            cur_incontext_answer = self.dataset[cur_incontext_id]["answer"]
            cur_incontext_image = self.images[cur_incontext_image_id]["image"]
            cur_incontext_image = Image.open(BytesIO(base64.urlsafe_b64decode(cur_incontext_image))).convert("RGB")
            cur_incontext_patch_image = (
                self.patch_resize_transform(cur_incontext_image) if type != "positioning" else None
            ).unsqueeze(0).unsqueeze(0)
            cur_incontext_patch_mask = torch.tensor([True]).unsqueeze(0)
            if len(patch_images) == 0:
                patch_images = cur_incontext_patch_image
                patch_masks = torch.tensor([True]).unsqueeze(0)
            else:
                patch_images = torch.cat((patch_images,cur_incontext_patch_image))
                patch_masks = torch.cat((patch_masks,cur_incontext_patch_mask))

            cur_incontext_instruction = self.pre_question(cur_incontext_instruction, self.max_src_length)
            cur_incontext_instruction = cur_incontext_instruction.strip("<image>")
            cur_incontext_answer = cur_incontext_answer.strip().replace("#", " ")
            cur_incontext_answer = self.pre_answer(cur_incontext_answer, self.max_tgt_length)
            cur_incontext_text = f"<image>User: {cur_incontext_instruction} GPT:<answer> {cur_incontext_answer}<|endofchunk|>"
            incontext_text += cur_incontext_text
            

        query_image = self.images[image_ids]["image"]
        query_image = Image.open(BytesIO(base64.urlsafe_b64decode(query_image))).convert("RGB")
        query_image = (
            self.patch_resize_transform(query_image) if type != "positioning" else None
        ).unsqueeze(0).unsqueeze(0)
        patch_images = torch.cat((patch_images,query_image))
        patch_masks = torch.cat((patch_masks,torch.tensor([True]).unsqueeze(0)))

        instruction = self.pre_question(instruction, self.max_src_length)
        instruction = instruction.strip("<image>")
        answer = answer.strip().replace("#", " ")
        answer = self.pre_answer(answer, self.max_tgt_length)
        query_text = f"<image>User: {instruction} GPT:<answer> {answer}<|endofchunk|>"

        src_text = self.tokenizer(
            f"{incontext_text}{query_text}",
            return_tensors="pt",
            add_special_tokens=False,
        )

        conf = torch.tensor([1.0])

        src_item = src_text["input_ids"].squeeze(0)
        src_item_mask = src_text["attention_mask"].squeeze(0)
        conf = torch.tensor([conf])

        src_item = torch.cat([self.bos_item, src_item, self.eos_item])
        src_item_mask = torch.cat([self.bos_mask, src_item_mask, self.eos_mask])

        example = {
            "id": instruction_id,
            "source": src_item,
            "text_mask": src_item_mask,
            "patch_images": patch_images,
            "patch_masks": patch_masks,
            "conf": conf,
        }

        return example

    def __len__(self):
        return len(self.train_data_list)

    def __getitem__(self, index):
        with numpy_seed(self.seed, self.epoch):
            pair_sample = self.process_image_text_pair(index)
            # if dataset is not supported
            if pair_sample is None:
                return self.__getitem__(index + 1)
        return pair_sample

    def collate(self, samples):
        """Merge samples of different tasks to form two mini-batches.
        Args:
            samples (List[Tuple]): samples to collate
        Returns:
            Tuple[dict]: two mini-batch containing the data of different tasks
        """

        samples_v1 = []  # containing image-text pairs
        for sample_tuple in samples:
            samples_v1.append(sample_tuple)
        
        # import pdb;pdb.set_trace()
        res_v1 = collate_fn(
            samples_v1,
            pad_idx=self.tokenizer.pad_token_id,
            eos_idx=self.tokenizer.eos_token_id,
        )
        return res_v1


if __name__ == "__main__":
    from PIL import Image, ImageFile
    from io import BytesIO
    import base64
    from tqdm import tqdm
    import json
    import argparse
    import sys
    sys.path.append("/mnt/lustre/yhzhang/Otter/")
    from flamingo.modeling_flamingo import FlamingoForConditionalGeneration


    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--multi_instruct_path",
        type=str,
        help="path to multi_instruct dataset, this should be a glob pattern such as vision_language_examples.tsv",
    )
    parser.add_argument("--offline", action="store_true")

    args = parser.parse_args()

    args.multi_instruct_path = "/mnt/lustre/yhzhang/data/LLaVA-Instruct-150K/complex_reasoning_77k/complex_reasoning_77k_text2text.json"
    args.images_path = "/mnt/lustre/yhzhang/data/LLaVA-Instruct-150K/llava_images.json"
    args.train_config_path = "/mnt/lustre/yhzhang/data/LLaVA-Instruct-150K/complex_reasoning_77k/complex_reasoning_77k_text2text_train.json"
    args.max_src_length = 256
    args.max_tgt_length = 256
    args.task = "pretrain"
    args.pretrain_seed = 0
    args.patch_image_size = 224

    from transformers import LlamaTokenizer
    
    with open( "/mnt/lustre/yhzhang/weights/openflamingo_9b_hf/config.json") as f:
        config = json.load(f)

    tokenizer = LlamaTokenizer.from_pretrained(
           "luodian/llama-7b-hf"
        )

    # add <answer> token to tokenizer
    tokenizer.add_special_tokens(
        {"additional_special_tokens": ["<|endofchunk|>", "<image>", "<answer>"]}
    )

    tokenizer.add_special_tokens({"pad_token": "<PAD>"})

    args.tokenizer = tokenizer

    test_dataset = UnifyDataset(args)

    uniq_id_dict = {}
    samples = []
    counter = 0
    for _ in tqdm(test_dataset):
        if counter > 2:
            break
        counter +=1
        samples.append(_)
    cur_data = test_dataset.collate(samples)
    import pdb;pdb.set_trace()
        # import pdb;pdb.set_trace()
        # uniq_id, image, caption, question, refs, gt_objects, dataset_name, type = _
        # # index = random.choice(positive_caption_dict[uniq_id])
        # # prompt_uniq_id, prompt_image, prompt_caption, prompt_question, prompt_refs, prompt_gt_objects, prompt_dataset_name, prompt_type = test_dataset.get_prompt_item(int(index))
        # uniq_id, image, caption, question, refs, gt_objects, dataset_name, type = _
        # if uniq_id not in uniq_id_dict:
        #     uniq_id_dict[uniq_id] = 0

        # print(uniq_id, image, caption, question, refs, gt_objects, dataset_name, type)
