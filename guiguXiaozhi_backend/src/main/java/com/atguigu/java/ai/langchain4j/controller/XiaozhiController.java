package com.atguigu.java.ai.langchain4j.controller;

import com.atguigu.java.ai.langchain4j.assistant.XiaozhiAgent;
import com.atguigu.java.ai.langchain4j.bean.ChatForm;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Flux;

@Tag(name = "硅谷小智")
@RestController
@RequestMapping("/xiaozhi")
public class XiaozhiController {

    @Autowired
    private XiaozhiAgent xiaozhiAgent;

    /** 空响应时自动重试的次数（本地 Ollama 偶发返回空响应） */
    @Value("${xiaozhi.chat.max-empty-retries:2}")
    private int maxEmptyRetries;

    @Operation(summary = "对话")
    @PostMapping(value = "/chat", produces = "text/stream;charset=utf-8")
    public Flux<String> chat(@RequestBody ChatForm chatForm) {
        return chatWithRetry(chatForm, 0);
    }

    /**
     * 调用智能体；若本地模型返回了空响应，则自动重试，
     * 保证前端永远不会收到一个空白的回答。
     */
    private Flux<String> chatWithRetry(ChatForm chatForm, int attempt) {
        return xiaozhiAgent.chat(chatForm.getMemoryId(), chatForm.getMessage())
                .switchIfEmpty(attempt < maxEmptyRetries
                        ? Flux.defer(() -> chatWithRetry(chatForm, attempt + 1))
                        : Flux.just("抱歉，本次对话没有生成有效内容，请换一种说法再试一次。"));
    }
}
