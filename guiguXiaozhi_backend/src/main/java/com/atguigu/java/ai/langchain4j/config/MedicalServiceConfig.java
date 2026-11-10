package com.atguigu.java.ai.langchain4j.config;

import com.atguigu.java.ai.langchain4j.service.DoctorService;
import com.atguigu.java.ai.langchain4j.service.SlotService;
import com.atguigu.java.ai.langchain4j.service.impl.VectorStoreDoctorService;
import com.atguigu.java.ai.langchain4j.service.mock.MockSlotService;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.store.embedding.EmbeddingStore;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 院内系统装配。
 * <p>
 * <b>哪些是 mock、哪些是真实调用：</b>
 * <ul>
 *   <li><b>号源 / 排班查询</b>：真实环境要调医院的 HIS 或统一挂号平台，本地调不通，
 *       因此用 {@link MockSlotService} 在本地内存里模拟排班规则。</li>
 *   <li><b>专家名录</b>：知识库与向量库（Milvus）都能正常调���，
 *       所以直接检索本地向量库实现（{@link VectorStoreDoctorService}），<b>不做 mock</b>。</li>
 * </ul>
 * <p>
 * 真实改造步骤（只需替换对应 Bean，工具层代码不用动）：
 * <ol>
 *   <li>新增 {@code HisSlotService implements SlotService}，在方法里调用医院挂号平台接口；</li>
 *   <li>若院方提供医生主数据接口，再新增 {@code HisDoctorService implements DoctorService}；
 *       否则沿用向量库实现即可。</li>
 *   <li>把下面两个 Bean 换成真实实现（用 {@code @Primary} 覆盖亦可）。</li>
 * </ol>
 */
@Configuration
public class MedicalServiceConfig {

    /** 专家名单所在的知识库文件名（向量库检索时按文件名过滤） */
    @Value("${xiaozhi.knowledge.doctor-roster-file:医生列表.md}")
    private String doctorRosterFile;

    @Bean
    public DoctorService doctorService(EmbeddingModel embeddingModel,
                                       EmbeddingStore<TextSegment> embeddingStore) {
        // 专家名单直接查本地向量库（Milvus），属于真实调用
        return new VectorStoreDoctorService(embeddingModel, embeddingStore, doctorRosterFile);
    }

    /**
     * 号源服务：调不通的外部接口，这里用 Mock 实现。
     */
    @Bean
    public SlotService slotService(DoctorService doctorService) {
        return new MockSlotService(doctorService);
    }
}
