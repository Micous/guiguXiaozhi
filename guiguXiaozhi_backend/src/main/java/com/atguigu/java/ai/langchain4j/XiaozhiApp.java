package com.atguigu.java.ai.langchain4j;
import dev.langchain4j.ollama.spring.AutoConfig;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 排除 Ollama 的自动装配：本项目在 OllamaConfig 中显式声明模型 Bean，
 * 以便精确控制模型名称与参数（同时避免出现重复 Bean）。
 */
@SpringBootApplication(exclude = AutoConfig.class)
public class XiaozhiApp {
    public static void main(String[] args) {
        SpringApplication.run(XiaozhiApp.class, args);
    }
}