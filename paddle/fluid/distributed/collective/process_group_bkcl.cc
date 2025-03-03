// Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
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

#include "paddle/fluid/distributed/collective/process_group_bkcl.h"

#include "paddle/common/errors.h"
#include "paddle/fluid/distributed/collective/bkcl_tools.h"
#include "paddle/fluid/distributed/collective/common.h"
#include "paddle/fluid/framework/convert_utils.h"
#include "paddle/fluid/platform/device/xpu/bkcl_helper.h"
#include "paddle/fluid/platform/device/xpu/xpu_info.h"
#include "paddle/phi/api/lib/data_transform.h"
#include "paddle/phi/api/lib/utils/allocator.h"
#include "paddle/phi/core/device_context.h"
#include "paddle/phi/core/distributed/check/static_check.h"
#include "paddle/phi/core/distributed/comm_context_manager.h"
#include "paddle/phi/core/enforce.h"

namespace paddle {
namespace distributed {
using XPUDeviceContext = paddle::platform::XPUDeviceContext;

ProcessGroupBKCL::BKCLTask::BKCLTask(const Place& place,
                                     int rank,
                                     CommType comm_type,
                                     bool sync_op,
                                     bool use_calc_stream)
    : TaskStream(rank, comm_type, sync_op, use_calc_stream), place_(place) {
  comm_event_ = std::make_shared<XPUEventManager>();
}

ProcessGroupBKCL::BKCLTask::~BKCLTask() {}

bool ProcessGroupBKCL::BKCLTask::IsCompleted() {
  LOG_FIRST_N(WARNING, 1) << "XPU do not support event query now.";
  return true;
}

// TODO(sheniang03): Add timeout for wait, now timeout unused
bool ProcessGroupBKCL::BKCLTask::Wait(std::chrono::milliseconds timeout) {
  // Warning here when use calc stream but also invoke waiting explicitly.
  if (UseCalcStream()) {
    VLOG(3) << "Warning: The communication is on calc stream, wait here is "
               "useless.";
    return true;
  }

  const auto* calc_ctx = static_cast<XPUContext*>(
      platform::DeviceContextPool::Instance().Get(place_));
  comm_event_->Block(*calc_ctx);

  if (barrier_) {
    // If we use the work to do barrier, we should block cpu

    // TODO(zhangxiaoci) There is no such function that can sync entire device
    // for xpu (for now), so all we can do is sync whatever stream that we know
    // and hope for the best. Note that for correctness the communication stream
    // needs to be in sync mode.
    platform::XPUDeviceGuard guard(place_.GetDeviceId());
    xpu_wait();
    calc_ctx->Wait();
  }
  return true;
}

// Same as Wait
void ProcessGroupBKCL::BKCLTask::Synchronize() { Wait(kWaitTimeout); }

ProcessGroupBKCL::ProcessGroupBKCL(
    const std::shared_ptr<phi::distributed::Store>& store,
    int rank,
    int size,
    int gid)
    : ProcessGroupWithStream(rank, size, gid), store_(store) {}

void ProcessGroupBKCL::GroupStart() {
  PADDLE_ENFORCE_XPU_SUCCESS(bkcl_group_start());
}

void ProcessGroupBKCL::GroupEnd() {
  PADDLE_ENFORCE_XPU_SUCCESS(bkcl_group_end());
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::Recv(
    phi::DenseTensor* tensor,
    int src_rank,
    int64_t offset,
    int64_t numel,
    bool sync_op,
    bool use_calc_stream) {
  // numel > 0 indicates the tensor need to be sliced
  phi::DenseTensor partial_tensor;
  if (numel > 0) {
    partial_tensor = GetPartialTensor(*tensor, offset, numel);
    tensor = &partial_tensor;
  }

  return Point2Point(
      [&](phi::distributed::BKCLCommContext* comm_context,
          XPUStream stream,
          int rank_in_group) {
        VLOG(3) << "bkcl_recv "
                << "recvbuff: " << tensor->data()
                << ", count: " << tensor->numel() << ", datatype: "
                << BKCLDTypeToString(phi::ToBKCLDataType(tensor->dtype()))
                << ", src_in_group: " << src_rank
                << ", bkcl_comm: " << comm_context->GetBKCLComm()
                << ", stream: " << stream
                << ", rank_in_group: " << rank_in_group << ", nranks: " << size_
                << ", offset: " << offset << ", sync_op: " << sync_op
                << ", use_calc_stream: " << use_calc_stream;
        comm_context->Recv(tensor, tensor->numel(), rank_in_group, stream);
      },
      src_rank,
      *tensor,
      CommType::RECV,
      sync_op,
      use_calc_stream);
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::Send(
    const phi::DenseTensor& tensor,
    int dst_rank,
    int64_t offset,
    int64_t numel,
    bool sync_op,
    bool use_calc_stream) {
  auto tensor_tmp =
      paddle::experimental::CheckAndTrans2NewContiguousTensor(tensor);
  // numel > 0 indicates the tensor need to be sliced
  const phi::DenseTensor& tensor_maybe_partial =
      numel > 0 ? GetPartialTensor(tensor_tmp, offset, numel) : tensor_tmp;

  return Point2Point(
      [&](phi::distributed::BKCLCommContext* comm_context,
          XPUStream stream,
          int rank_in_group) {
        VLOG(3) << "bkcl_send "
                << "sendbuff: " << tensor_maybe_partial.data()
                << ", count: " << tensor_maybe_partial.numel() << ", datatype: "
                << BKCLDTypeToString(
                       phi::ToBKCLDataType(tensor_maybe_partial.dtype()))
                << ", dst_in_group: " << dst_rank
                << ", bkcl_comm: " << comm_context->GetBKCLComm()
                << ", stream: " << stream
                << ", rank_in_group: " << rank_in_group << ", nranks: " << size_
                << ", offset: " << offset << ", sync_op: " << sync_op
                << ", use_calc_stream: " << use_calc_stream;
        comm_context->Send(tensor_maybe_partial,
                           tensor_maybe_partial.numel(),
                           rank_in_group,
                           stream);
      },
      dst_rank,
      tensor_maybe_partial,
      CommType::SEND,
      sync_op,
      use_calc_stream);
}

std::shared_ptr<ProcessGroupBKCL::BKCLTask> ProcessGroupBKCL::CreateTask(
    const Place& place,
    int rank,
    CommType comm_type,
    bool is_sync,
    bool use_calc_stream) {
  return std::make_shared<ProcessGroupBKCL::BKCLTask>(
      place, rank, comm_type, is_sync, use_calc_stream);
}

void ProcessGroupBKCL::BroadcastUniqueBKCLID(BKCLUniqueId* bkcl_id) {
  auto key = "ProcessGroupBKCL/bkcl_ids/" + std::to_string(gid_) + "/0";
  if (rank_ == 0) {
    auto id = std::vector<uint8_t>(
        reinterpret_cast<uint8_t*>(bkcl_id),
        reinterpret_cast<uint8_t*>(bkcl_id) + BKCL_UNIQUE_ID_BYTES);
    store_->set(key, id);
  } else {
    const auto& ret = store_->get(key);
    std::memcpy(bkcl_id, ret.data(), ret.size());
  }
}

void ProcessGroupBKCL::CreateBKCLEnvCache(const Place& place,
                                          const std::string& place_key) {
  platform::XPUDeviceGuard guard(place.GetDeviceId());

  VLOG(3) << "init bkcl rank: " << rank_ << ", nranks: " << size_
          << ", place: " << place_key;

  phi::distributed::CommContextManager::CreateBKCLCommContext(
      store_, std::to_string(gid_), rank_, size_);

  calc_event_ = std::make_shared<XPUEventManager>();
  auto* calc_ctx = static_cast<phi::XPUContext*>(
      platform::DeviceContextPool::Instance().Get(place));
  // must use XPUDeviceContext here to make sure XPUContext::Init() is called
  auto comm_ctx = std::make_unique<XPUDeviceContext>(place);
  auto bkcl_comm_ctx = this->GetCommContext();
  comm_ctx->SetBkclContext(bkcl_comm_ctx->GetBKCLComm());

  // set allocator
  comm_ctx->SetAllocator(memory::allocation::AllocatorFacade::Instance()
                             .GetAllocator(place)
                             .get());
  // comm context creates a separate XPU stream for communication
  comm_ctx->CreateStream();

  place_to_calc_ctx_[place_key] = calc_ctx;
  place_to_comm_ctx_[place_key] = std::move(comm_ctx);
}

void ProcessGroupBKCL::SyncCalcStream(const Place& place) {
  const std::string& key = GetKeyFromPlace(place);
  const auto* calc_ctx = place_to_calc_ctx_[key];
  const auto* comm_ctx = place_to_comm_ctx_[key].get();
  calc_event_->Record(*calc_ctx);
  calc_event_->Block(*comm_ctx);
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::Collective(
    std::function<void(phi::distributed::BKCLCommContext*, XPUStream)> fn,
    const phi::DenseTensor& tensor,
    CommType op_type,
    bool sync_op,
    bool use_calc_stream) {
  const auto& place = tensor.place();
  const auto& key = GetKeyFromPlace(place);

  platform::XPUDeviceGuard xpu_guard(place);

  if (!calc_event_ ||
      (place_to_comm_ctx_.find(key) == place_to_comm_ctx_.end())) {
    CreateBKCLEnvCache(place, key);
  }

  if (!use_calc_stream) {
    SyncCalcStream(place);
  }

  auto task = CreateTask(place, rank_, op_type, sync_op, use_calc_stream);

  const auto* calc_ctx = place_to_calc_ctx_.at(key);
  const auto& comm_ctx = place_to_comm_ctx_.at(key);
  auto bkcl_stream = use_calc_stream ? calc_ctx->stream() : comm_ctx->stream();

  auto bkcl_comm_ctx = this->GetCommContext();

  fn(bkcl_comm_ctx, bkcl_stream);

  if (!use_calc_stream) {
    PADDLE_ENFORCE_NOT_NULL(
        comm_ctx.get(), platform::errors::Fatal("comm context is nullptr."));
    task->comm_event_->Record(*comm_ctx.get());
  }

  if (sync_op) {
    task->Wait();
  }

  return task;
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::Point2Point(
    std::function<void(phi::distributed::BKCLCommContext*, XPUStream, int)> fn,
    int peer,
    const phi::DenseTensor& tensor,
    CommType comm_type,
    bool sync_op,
    bool use_calc_stream) {
  auto tensor_tmp =
      paddle::experimental::CheckAndTrans2NewContiguousTensor(tensor);
  const auto& place = tensor_tmp.place();

  int p2p_target_rank = peer;
  std::string key = GetKeyFromPlace(place);

  platform::XPUDeviceGuard xpu_guard(place);

  if (place_to_comm_ctx_.find(key) == place_to_comm_ctx_.end()) {
    CreateBKCLEnvCache(place, key);
  }

  if (!use_calc_stream) {
    SyncCalcStream(place);
  }

  auto task = CreateTask(place, rank_, comm_type, sync_op, use_calc_stream);
  const auto* calc_ctx = place_to_calc_ctx_.at(key);
  const auto& comm_ctx = place_to_comm_ctx_.at(key);
  auto bkcl_stream = use_calc_stream ? calc_ctx->stream() : comm_ctx->stream();

  auto bkcl_comm_ctx = this->GetCommContext();

  fn(bkcl_comm_ctx, bkcl_stream, p2p_target_rank);

  if (!use_calc_stream) {
    PADDLE_ENFORCE_NOT_NULL(
        comm_ctx.get(), platform::errors::Fatal("comm context is nullptr."));
    task->comm_event_->Record(*comm_ctx.get());
  }

  if (sync_op) {
    task->Wait();
  }

  return task;
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::AllReduce(
    phi::DenseTensor* out_tensor,
    const phi::DenseTensor& in_tensor,
    const AllreduceOptions& opts,
    bool sync_op,
    bool use_calc_stream) {
  auto tensor_tmp =
      paddle::experimental::CheckAndTrans2NewContiguousTensor(in_tensor);
  return Collective(
      [&](phi::distributed::BKCLCommContext* comm_context, XPUStream stream) {
        VLOG(3) << "bkcl_all_reduce"
                << "sendbuff: " << tensor_tmp.data()
                << ", recvbuff: " << out_tensor->data()
                << ", count: " << tensor_tmp.numel() << ", datatype: "
                << BKCLDTypeToString(phi::ToBKCLDataType(tensor_tmp.dtype()))
                << ", redop: " << ToBKCLRedType(opts.reduce_op)
                << ", bkcl_comm: " << comm_context->GetBKCLComm()
                << ", stream: " << stream << ", rank_in_group: " << rank_
                << ", nranks: " << size_ << ", sync_op: " << sync_op
                << ", use_calc_stream: " << use_calc_stream;

        comm_context->AllReduce(
            out_tensor, tensor_tmp, ToBKCLRedType(opts.reduce_op), stream);
      },
      tensor_tmp,
      CommType::ALLREDUCE,
      sync_op,
      use_calc_stream);
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::Broadcast(
    phi::DenseTensor* out_tensor,
    const phi::DenseTensor& in_tensor,
    const BroadcastOptions& opts,
    bool sync_op,
    bool use_calc_stream) {
  auto tensor_tmp =
      paddle::experimental::CheckAndTrans2NewContiguousTensor(in_tensor);
  return Collective(
      [&](phi::distributed::BKCLCommContext* comm_context, XPUStream stream) {
        int root = opts.source_rank + opts.source_root;

        VLOG(3) << "bkcl_broadcast "
                << "sendbuff: " << tensor_tmp.data()
                << ", recvbuff: " << out_tensor->data()
                << ", count: " << tensor_tmp.numel() << ", datatype: "
                << BKCLDTypeToString(phi::ToBKCLDataType(tensor_tmp.dtype()))
                << ", root: " << root
                << ", bkcl_comm: " << comm_context->GetBKCLComm()
                << ", stream: " << stream << ", rank_in_group: " << rank_
                << ", nranks: " << size_ << ", sync_op: " << sync_op
                << ", use_calc_stream: " << use_calc_stream;
        comm_context->Broadcast(out_tensor, tensor_tmp, root, stream);
      },
      tensor_tmp,
      CommType::BROADCAST,
      sync_op,
      use_calc_stream);
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::AllGather(
    phi::DenseTensor* out_tensor,
    const phi::DenseTensor& in_tensor,
    int64_t offset,
    int64_t numel,
    bool sync_op,
    bool use_calc_stream) {
  auto tensor_tmp =
      paddle::experimental::CheckAndTrans2NewContiguousTensor(in_tensor);
  const phi::DenseTensor& in_tensor_maybe_partial =
      numel > 0 ? GetPartialTensor(tensor_tmp, offset, numel) : tensor_tmp;
  phi::distributed::CommStaticCheck::GatherLikeShape(*out_tensor,
                                                     in_tensor_maybe_partial,
                                                     /*dst_rank*/ rank_,
                                                     /*cur_rank*/ rank_,
                                                     size_,
                                                     phi::AllocationType::XPU);
  return Collective(
      [&](phi::distributed::BKCLCommContext* comm_context, XPUStream stream) {
        VLOG(3) << "bkcl_all_gather "
                << "sendbuff: " << in_tensor_maybe_partial.data()
                << ", recvbuff: " << out_tensor->data()
                << ", count: " << in_tensor_maybe_partial.numel()
                << ", datatype: "
                << BKCLDTypeToString(phi::ToBKCLDataType(in_tensor.dtype()))
                << ", bkcl_comm: " << comm_context->GetBKCLComm()
                << ", stream: " << stream << ", rank_in_group: " << rank_
                << ", nranks: " << size_ << ", offset: " << offset
                << ", sync_op: " << sync_op
                << ", use_calc_stream: " << use_calc_stream;

        comm_context->AllGather(out_tensor, in_tensor_maybe_partial, stream);
      },
      in_tensor_maybe_partial,
      CommType::ALLGATHER,
      sync_op,
      use_calc_stream);
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::Reduce(
    phi::DenseTensor* out_tensor,
    const phi::DenseTensor& in_tensor,
    const ReduceOptions& opts,
    bool sync_op,
    bool use_calc_stream) {
  auto tensor_tmp =
      paddle::experimental::CheckAndTrans2NewContiguousTensor(in_tensor);
  return Collective(
      [&](phi::distributed::BKCLCommContext* comm_context, XPUStream stream) {
        VLOG(3) << "bkcl_reduce "
                << "sendbuff: " << tensor_tmp.data()
                << ", recvbuff: " << out_tensor->data()
                << ", count: " << tensor_tmp.numel() << ", datatype: "
                << BKCLDTypeToString(phi::ToBKCLDataType(tensor_tmp.dtype()))
                << ", redop: "
                << BKCLRedTypeToString(ToBKCLRedType(opts.reduce_op))
                << ", root: " << opts.root_rank
                << ", bkcl_comm: " << comm_context->GetBKCLComm()
                << ", stream: " << stream << ", rank_in_group: " << rank_
                << ", nranks: " << size_ << ", sync_op: " << sync_op
                << ", use_calc_stream: " << use_calc_stream;
        comm_context->Reduce(out_tensor,
                             tensor_tmp,
                             ToBKCLRedType(opts.reduce_op),
                             opts.root_rank,
                             stream);
      },
      tensor_tmp,
      CommType::REDUCE,
      sync_op,
      use_calc_stream);
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::ReduceScatter(
    phi::DenseTensor* out_tensor,
    const phi::DenseTensor& in_tensor,
    const ReduceScatterOptions& opts,
    bool sync_op,
    bool use_calc_stream) {
  auto tensor_tmp =
      paddle::experimental::CheckAndTrans2NewContiguousTensor(in_tensor);
  return Collective(
      [&](phi::distributed::BKCLCommContext* comm_context, XPUStream stream) {
        VLOG(3) << "bkcl_reduce_scatter "
                << "sendbuff: " << tensor_tmp.data()
                << ", recvbuff: " << out_tensor->data()
                << ", count: " << tensor_tmp.numel() << ", datatype: "
                << BKCLDTypeToString(phi::ToBKCLDataType(tensor_tmp.dtype()))
                << ", redop: "
                << BKCLRedTypeToString(ToBKCLRedType(opts.reduce_op))
                << ", bkcl_comm: " << comm_context->GetBKCLComm()
                << ", stream: " << stream << ", rank_in_group: " << rank_
                << ", nranks: " << size_ << ", sync_op: " << sync_op
                << ", use_calc_stream: " << use_calc_stream;
        comm_context->ReduceScatter(
            out_tensor, tensor_tmp, ToBKCLRedType(opts.reduce_op), stream);
      },
      tensor_tmp,
      CommType::REDUCE_SCATTER,
      sync_op,
      use_calc_stream);
}

std::shared_ptr<ProcessGroup::Task> ProcessGroupBKCL::Barrier(
    const BarrierOptions& opts) {
  PADDLE_ENFORCE_GE(opts.device_id,
                    0,
                    platform::errors::PreconditionNotMet(
                        "The barrier device id must greater or equal than 0."));
  platform::XPUPlace place(opts.device_id);
  auto allocator = std::unique_ptr<phi::Allocator>(
      new paddle::experimental::DefaultAllocator(place));
  phi::DenseTensorMeta meta(phi::DataType::FLOAT32, phi::DDim{1});
  phi::DenseTensor barrier_tensor{allocator.get(), meta};

  auto task = AllReduce(&barrier_tensor,
                        barrier_tensor,
                        {},
                        /*sync_op*/ true,
                        /*use_calc_stream*/ false);
  auto bkcl_task = dynamic_cast<BKCLTask*>(task.get());
  bkcl_task->barrier_ = true;
  return task;
}

phi::DeviceContext* ProcessGroupBKCL::GetDeviceContext(
    const Place& place) const {
  return GetDeviceContext(place, /*use_calc_stream*/ false);
}

phi::DeviceContext* ProcessGroupBKCL::GetDeviceContext(
    const Place& place, bool use_calc_stream) const {
  const std::string& key = GetKeyFromPlace(place);
  if (use_calc_stream) {
    const auto& iter = place_to_calc_ctx_.find(key);
    return iter->second;
  } else {
    const auto& iter = place_to_comm_ctx_.find(key);
    PADDLE_ENFORCE_NE(iter,
                      place_to_comm_ctx_.end(),
                      platform::errors::InvalidArgument(
                          "Cannot find device context in process group."));
    return iter->second.get();
  }
}

std::shared_ptr<ProcessGroupBKCL> ProcessGroupBKCL::CreateProcessGroupBKCL(
    const std::shared_ptr<phi::distributed::Store>& store,
    int rank,
    int size,
    int gid) {
  auto process_group =
      std::make_shared<ProcessGroupBKCL>(store, rank, size, gid);
  ProcessGroupIdMap::GetInstance().emplace(gid, process_group);
  return process_group;
}

phi::distributed::BKCLCommContext* ProcessGroupBKCL::GetCommContext() {
  const auto& comm_context_manager =
      phi::distributed::CommContextManager::GetInstance();
  auto comm_context = static_cast<phi::distributed::BKCLCommContext*>(
      comm_context_manager.Get(std::to_string(this->gid_)));
  PADDLE_ENFORCE_NE(comm_context,
                    nullptr,
                    phi::errors::Unavailable("BKCLCommContext is nullptr"));
  return comm_context;
}

}  //  namespace distributed
}  //  namespace paddle
