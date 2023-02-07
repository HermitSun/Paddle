// Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include <brpc/channel.h>
#include <bthread/countdown_event.h>

#include <condition_variable>
#include <fstream>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>

namespace paddle {
namespace platform {

class RpcVocabulary {
 public:
  static RpcVocabulary& Instance() {
    static RpcVocabulary instance;
    return instance;
  }

  void Init(const std::string& path) {
    if (path_ == path) {
      return;
    }
    std::ifstream vocab_file(path);
    std::string word;
    int id;
    while (vocab_file >> word >> id) {
      vocab_.emplace(id, word);
    }
  }

  bool Contains(int id) { return vocab_.count(id) > 0; }

  // NOTE: an exception will be raised if id not exist
  std::string Get(int id) { return vocab_.at(id); }

 private:
  std::string path_;
  std::unordered_map<int, std::string> vocab_;
};

class RpcRequestStore {
 public:
  static RpcRequestStore& Instance() {
    static RpcRequestStore instance;
    return instance;
  }

  int GetRequestId() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (request_id_ == INT32_MAX) {
      request_id_ = 0;
    } else {
      ++request_id_;
    }
    return request_id_;
  }

  std::shared_ptr<bthread::CountdownEvent> GetEvent(int request_id) {
    return id_to_event_map_[request_id];
  }

  bool GetErrorCode(int request_id) { return id_to_err_map_[request_id]; }

  std::string GetResponse(int request_id) {
    return id_to_resp_map_[request_id];
  }

  void InsertEvent(int request_id,
                   const std::shared_ptr<bthread::CountdownEvent>& event) {
    if (request_id == 0) {
      LOG(WARNING) << "Total num of requests have exceeded int limits.";
    }
    id_to_event_map_.emplace(request_id, event);
  }

  void InsertErrorCode(int request_id, int error_code) {
    if (request_id == 0) {
      LOG(WARNING) << "Total num of requests have exceeded int limits.";
    }
    id_to_err_map_.emplace(request_id, error_code);
  }

  void InsertResponse(int request_id, const std::string& resp) {
    if (request_id == 0) {
      LOG(WARNING) << "Total num of requests have exceeded int limits.";
    }
    id_to_resp_map_.emplace(request_id, resp);
  }

 private:
  std::mutex mutex_;
  int request_id_;
  std::unordered_map<int, std::shared_ptr<bthread::CountdownEvent>>
      id_to_event_map_;
  std::unordered_map<int, int> id_to_err_map_;
  std::unordered_map<int, std::string> id_to_resp_map_;
};

int RpcSend(const std::string& url,
            const std::string& query,
            void (*payload_builder)(brpc::Controller*, int, const std::string&),
            void (*response_handler)(brpc::Controller*,
                                     int,
                                     std::shared_ptr<bthread::CountdownEvent>),
            brpc::HttpMethod http_method = brpc::HttpMethod::HTTP_METHOD_POST,
            int timeout_ms = 10000,
            int max_retry = 3);

}  // namespace platform
}  // namespace paddle
