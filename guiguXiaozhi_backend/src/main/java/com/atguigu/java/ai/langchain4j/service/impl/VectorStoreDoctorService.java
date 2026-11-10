package com.atguigu.java.ai.langchain4j.service.impl;

import com.atguigu.java.ai.langchain4j.service.DoctorInfo;
import com.atguigu.java.ai.langchain4j.service.DoctorService;
import dev.langchain4j.data.embedding.Embedding;
import dev.langchain4j.data.segment.TextSegment;
import dev.langchain4j.model.embedding.EmbeddingModel;
import dev.langchain4j.store.embedding.EmbeddingMatch;
import dev.langchain4j.store.embedding.EmbeddingSearchRequest;
import dev.langchain4j.store.embedding.EmbeddingSearchResult;
import dev.langchain4j.store.embedding.EmbeddingStore;
import dev.langchain4j.store.embedding.filter.MetadataFilterBuilder;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * 专家名录服务 —— <b>直接检索本地向量库（Milvus）</b>实现，不做任何 mock。
 * <p>
 * 专家名单来自知识库 {@code knowledge/医生列表.md}，启动时已经被切分、向量化写入 Milvus；
 * 这里通过向量检索拿到该科室的专家段落并解析出「姓名 / 职称 / 擅长方向」。
 * <p>
 * <b>扩展点</b>：如果院方后续提供真实的医生主数据接口（HIS），可以另写一个
 * {@code HisDoctorService implements DoctorService} 并在 {@code MedicalServiceConfig} 中
 * 替换掉本实现，工具层代码无需改动。
 */
public class VectorStoreDoctorService implements DoctorService {

    private static final Logger log = LoggerFactory.getLogger(VectorStoreDoctorService.class);

    /** 专家名单所在的知识库文件 */
    private final String rosterFileName;

    private final EmbeddingModel embeddingModel;
    private final EmbeddingStore<TextSegment> embeddingStore;

    public VectorStoreDoctorService(EmbeddingModel embeddingModel,
                                    EmbeddingStore<TextSegment> embeddingStore,
                                    String rosterFileName) {
        this.embeddingModel = embeddingModel;
        this.embeddingStore = embeddingStore;
        this.rosterFileName = rosterFileName;
    }

    @Override
    public List<String> listDepartments() {
        // 专家名单文件里每个 “## 科室” 是一个独立段落，逐段检索代价高；
        // 这里直接复用缓存的科室索引（首次检索时填充）。
        return DepartmentIndex.get().departments(embeddingModel, embeddingStore, rosterFileName);
    }

    @Override
    public List<DoctorInfo> listDoctors(String department) {
        if (department == null || department.isBlank()) {
            return List.of();
        }
        return DepartmentIndex.get().doctorsOf(embeddingModel, embeddingStore, rosterFileName, department.trim());
    }

    @Override
    public Optional<DoctorInfo> findDoctor(String department, String doctorName) {
        if (doctorName == null || doctorName.isBlank()) {
            return Optional.empty();
        }
        return listDoctors(department).stream()
                .filter(d -> d.name().contains(doctorName.trim()) || doctorName.trim().contains(d.name()))
                .findFirst();
    }

    /**
     * 专家名单索引：把向量库中专家名单文件的所有段落解析成「科室 -> 医生列表」。
     * <p>
     * 首次使用时从 Milvus 拉取该文件的全部段落并解析，之后常驻内存。
     */
    static class DepartmentIndex {

        private static final DepartmentIndex INSTANCE = new DepartmentIndex();

        private final Map<String, List<DoctorInfo>> roster = new LinkedHashMap<>();
        private volatile boolean loaded = false;

        static DepartmentIndex get() {
            return INSTANCE;
        }

        synchronized List<String> departments(EmbeddingModel embeddingModel,
                                             EmbeddingStore<TextSegment> embeddingStore,
                                             String rosterFileName) {
            ensureLoaded(embeddingModel, embeddingStore, rosterFileName);
            return new ArrayList<>(roster.keySet());
        }

        synchronized List<DoctorInfo> doctorsOf(EmbeddingModel embeddingModel,
                                                EmbeddingStore<TextSegment> embeddingStore,
                                                String rosterFileName,
                                                String department) {
            ensureLoaded(embeddingModel, embeddingStore, rosterFileName);

            // 1) 精确匹配
            for (Map.Entry<String, List<DoctorInfo>> entry : roster.entrySet()) {
                if (entry.getKey().equals(department)) {
                    return entry.getValue();
                }
            }
            // 2) 去掉常见后缀后匹配（“神经内科门诊” → “神经内科”）
            String stripped = department.replaceAll("(专家)?(普通)?门诊$", "").replaceAll("科$", "");
            for (Map.Entry<String, List<DoctorInfo>> entry : roster.entrySet()) {
                if (entry.getKey().equals(stripped)) {
                    return entry.getValue();
                }
            }
            // 3) 包含匹配，但要求长度接近，避免把无关说法匹配到某个科室
            for (Map.Entry<String, List<DoctorInfo>> entry : roster.entrySet()) {
                if (department.contains(entry.getKey()) && entry.getKey().length() * 2 >= department.length()) {
                    return entry.getValue();
                }
            }
            return List.of();
        }

        private void ensureLoaded(EmbeddingModel embeddingModel,
                                  EmbeddingStore<TextSegment> embeddingStore,
                                  String rosterFileName) {
            if (loaded) {
                return;
            }
            try {
                // 按文件名过滤，只召回专家名单文件的段落
                Embedding probe = embeddingModel.embed("北京协和医院 医生列表 出诊专家名单").content();
                EmbeddingSearchResult<TextSegment> result = embeddingStore.search(EmbeddingSearchRequest.builder()
                        .queryEmbedding(probe)
                        .maxResults(200)
                        .filter(MetadataFilterBuilder.metadataKey("file_name").isEqualTo(rosterFileName))
                        .build());
                for (EmbeddingMatch<TextSegment> match : result.matches()) {
                    parse(match.embedded().text());
                }
                loaded = true;
                log.info("专家名录索引加载完成，共 {} 个科室、{} 位医生",
                        roster.size(), roster.values().stream().mapToInt(List::size).sum());
            } catch (Exception e) {
                log.error("加载专家名录索引失败", e);
            }
        }

        /**
         * 解析形如：
         * <pre>
         * ## 神经内科
         * - 王笠，男，主任医师，神经内科。擅长：脑血管病、脑梗死的急性期治疗与二级预防、神经介入。
         * </pre>
         */
        private void parse(String text) {
            String currentDepartment = null;
            for (String line : text.split("\\R")) {
                String trimmed = line.trim();
                if (trimmed.startsWith("## ")) {
                    currentDepartment = trimmed.substring(3).trim();
                    roster.computeIfAbsent(currentDepartment, k -> new ArrayList<>());
                    continue;
                }
                if (currentDepartment == null || !trimmed.startsWith("- ")) {
                    continue;
                }
                String body = trimmed.substring(2).trim();
                String[] parts = body.split("，");
                if (parts.length < 3) {
                    continue;
                }
                String name = parts[0].trim();
                String title = parts[2].trim();
                String expertise = "";
                int marker = body.indexOf("擅长：");
                if (marker >= 0) {
                    expertise = body.substring(marker + "擅长：".length()).trim();
                }
                boolean exists = roster.get(currentDepartment).stream()
                        .anyMatch(d -> d.name().equals(name));
                if (!exists) {
                    roster.get(currentDepartment).add(new DoctorInfo(name, currentDepartment, title, expertise));
                }
            }
        }
    }
}
