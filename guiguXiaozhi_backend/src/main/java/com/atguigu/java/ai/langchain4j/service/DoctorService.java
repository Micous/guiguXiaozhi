package com.atguigu.java.ai.langchain4j.service;

import java.util.List;
import java.util.Optional;

/**
 * 科室与专家名录服务 —— 对接医院科室/医生主数据（HIS 主数据）的扩展点。
 * <p>
 * <b>当前实现是真实检索，不是 mock</b>：默认实现
 * {@link com.atguigu.java.ai.langchain4j.service.impl.VectorStoreDoctorService}
 * 直接检索本地向量库（Milvus）里的专家名单知识库文档。
 * <p>
 * 真实改造时：若院方提供医生主数据接口，新增
 * {@code HisDoctorService implements DoctorService} 并替换 Bean 装配即可。
 */
public interface DoctorService {

    /** 全部科室名称 */
    List<String> listDepartments();

    /** 某科室的专家列表 */
    List<DoctorInfo> listDoctors(String department);

    /** 在某科室内查找指定医生 */
    Optional<DoctorInfo> findDoctor(String department, String doctorName);
}
