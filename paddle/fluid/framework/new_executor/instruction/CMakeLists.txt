cc_library(
  instruction_base
  SRCS instruction_base.cc
       phi_kernel_instruction.cc
       legacy_kernel_instruction.cc
       if_instruction.cc
       assert_instruction.cc
       while_instruction.cc
       select_input_instruction.cc
       has_elements_instruction.cc
       tuple_push_instruction.cc
       tuple_pop_instruction.cc
       builtin_combine_instruction.cc
       instruction_util.cc
  DEPS framework_proto)

add_dependencies(instruction_base xxhash framework_proto)
if(WITH_MKLDNN)
  add_dependencies(instruction_base mkldnn)
endif()

if(WITH_CINN AND NOT CINN_ONLY)
  cc_library(
    cinn_jit_instruction NOT_FOR_INFER
    SRCS cinn_jit_instruction.cc
    DEPS cinnapi cinn_op_dialect cinn_runtime_dialect)
endif()
