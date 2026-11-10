package com.atguigu.java.ai.langchain4j.config;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.langchain4j.http.client.HttpClientBuilder;
import dev.langchain4j.http.client.jdk.JdkHttpClient;
import dev.langchain4j.model.chat.StreamingChatLanguageModel;
import dev.langchain4j.model.ollama.OllamaChatModel;
import dev.langchain4j.model.ollama.OllamaEmbeddingModel;
import dev.langchain4j.model.ollama.OllamaStreamingChatModel;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/**
 * 本地大模型配置：全部使用本机 Ollama 提供的模型，不依赖任何云端 API。
 * <p>
 * 对话 / 流式对话模型：qwen2.5 系列（支持 Function Calling，可驱动挂号工具），
 *   具体模型由配置项 xiaozhi.ollama.chat-model 指定，启动脚本会自动挑选本机已安装的版本。
 * 向量化模型：andersc/qwen3-embedding:0.6b（1024 维）
 */
@Configuration
public class OllamaConfig {

    private static final org.slf4j.Logger log = org.slf4j.LoggerFactory.getLogger(OllamaConfig.class);

    /** Ollama 服务地址 */
    @Value("${xiaozhi.ollama.base-url:http://localhost:11434}")
    private String baseUrl;

    /** 对话模型名称 */
    @Value("${xiaozhi.ollama.chat-model:qwen2.5:3b}")
    private String chatModelName;

    /** 向量化模型名称 */
    @Value("${xiaozhi.ollama.embedding-model:andersc/qwen3-embedding:0.6b}")
    private String embeddingModelName;

    /** 采样温度 */
    @Value("${xiaozhi.ollama.temperature:0.1}")
    private Double temperature;

    /** 请求超时时间（秒），本地推理较慢，需要放宽 */
    @Value("${xiaozhi.ollama.timeout-seconds:300}")
    private Long timeoutSeconds;

    /** 上下文窗口大小：Ollama 默认只有 4k，系统提示词 + 工具定义 + 知识库 + 对话历史很容易超出 */
    @Value("${xiaozhi.ollama.num-ctx:8192}")
    private Integer numCtx;

    /** 空响应自动重试次数 */
    @Value("${xiaozhi.ollama.max-retries:2}")
    private Integer maxRetries;

    /** 是否打印请求 / 响应日志，便于本地排查 */
    @Value("${xiaozhi.ollama.log-requests:false}")
    private Boolean logRequests;

    /**
     * 统一使用 JDK 自带的 HttpClient。
     * <p>
     * 原因：langchain4j-ollama 默认依赖的 Spring RestClient 客户端会使用
     * {@code StringHttpMessageConverter} 的默认字符集（ISO-8859-1）写请求体，
     * 导致系统提示词、知识库内容、工具名称中的中文全部被转成 "?"，
     * 模型收到的是乱码，因而无法正常理解用户意图。
     * JDK HttpClient 使用 {@code BodyPublishers.ofString(body)}，默认按 UTF-8 编码。
     */
    @Bean
    public HttpClientBuilder ollamaHttpClientBuilder() {
        return JdkHttpClient.builder()
                .connectTimeout(Duration.ofSeconds(30))
                .readTimeout(Duration.ofSeconds(timeoutSeconds));
    }

    /**
     * 解析实际使用的对话模型：优先使用配置项指定的模型，
     * 如果本机 Ollama 里没装该模型，则自动回退到已安装的 qwen2.5 系列模型，
     * 避免因为模型缺失导致服务起不来。
     */
    private String resolveChatModel() {
        List<String> installed = listInstalledModels();
        if (installed.isEmpty()) {
            log.warn("无法连接 Ollama ({})，将直接使用配置的模型 {}，请确认 Ollama 已启动", baseUrl, chatModelName);
            return chatModelName;
        }
        if (installed.contains(chatModelName)) {
            return chatModelName;
        }
        for (String candidate : List.of("qwen2.5:7b", "qwen2.5:3b", "qwen2.5:1.5b", "qwen3", "qwen2.5")) {
            if (installed.contains(candidate)) {
                log.warn("本机未安装模型 {}，自动回退到已安装的 {}", chatModelName, candidate);
                return candidate;
            }
        }
        log.warn("未找到可用的 qwen 模型，installed={}，仍使用 {}", installed, chatModelName);
        return chatModelName;
    }

    /** 查询本机 Ollama 已安装的模型列表 */
    private List<String> listInstalledModels() {
        List<String> names = new ArrayList<>();
        try {
            HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + "/api/tags"))
                    .timeout(Duration.ofSeconds(10))
                    .GET()
                    .build();
            HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
            JsonNode root = new ObjectMapper().readTree(response.body());
            for (JsonNode model : root.path("models")) {
                String name = model.path("name").asText(null);
                if (name != null && !name.isBlank()) {
                    names.add(name);
                }
            }
        } catch (Exception e) {
            log.warn("查询 Ollama 模型列表失败: {}", e.getMessage());
        }
        return names;
    }

    @Bean
    public OllamaChatModel ollamaChatModel() {
        return OllamaChatModel.builder()
                .baseUrl(baseUrl)
                .modelName(resolveChatModel())
                .temperature(temperature)
                .numCtx(numCtx)
                .timeout(Duration.ofSeconds(timeoutSeconds))
                .logRequests(logRequests)
                .logResponses(logRequests)
                .httpClientBuilder(ollamaHttpClientBuilder())
                .build();
    }

    @Bean
    public OllamaStreamingChatModel ollamaStreamingChatModel() {
        return OllamaStreamingChatModel.builder()
                .baseUrl(baseUrl)
                .modelName(resolveChatModel())
                .temperature(temperature)
                .numCtx(numCtx)
                .timeout(Duration.ofSeconds(timeoutSeconds))
                .logRequests(logRequests)
                .logResponses(logRequests)
                .httpClientBuilder(ollamaHttpClientBuilder())
                .build();
    }

    /**
     * 供 AI Service 使用的流式对话模型：外面包一层重试逻辑，
     * 规避 Ollama 在提示词缓存命中时偶发返回空响应的问题。
     */
    @Bean
    public StreamingChatLanguageModel xiaozhiStreamingChatModel(OllamaStreamingChatModel ollamaStreamingChatModel) {
        return new RetryStreamingChatModel(ollamaStreamingChatModel, maxRetries);
    }

    @Bean
    public OllamaEmbeddingModel ollamaEmbeddingModel() {
        return OllamaEmbeddingModel.builder()
                .baseUrl(baseUrl)
                .modelName(embeddingModelName)
                .timeout(Duration.ofSeconds(timeoutSeconds))
                .httpClientBuilder(ollamaHttpClientBuilder())
                .build();
    }
}
